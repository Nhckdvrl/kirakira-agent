"""Drift runner：把"一轮 Drift"跑成一次 agent run。

复用 kirakira 现有的 ``Agent`` loop 与 ``build_default_registry`` 工具集，
额外挂上 ``message_push`` / ``finish_drift`` 收尾工具。SKILL.md 正文作为
system prompt，Drift Briefing 作为首条消息注入。跑完把结果落到 drift.db，
并在主事件循环上投递草稿消息。

参考 akashic 的 `plugins/drift_flow` DriftTurnPipeline，MVP 压平为直接的一次 run。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List

from kirakira_agent.agent import Agent
from kirakira_agent.bus import MessageBus
from kirakira_agent.drift.skills import DriftSkill, discover_skills, ensure_example_skill
from kirakira_agent.drift.state import DriftStateStore
from kirakira_agent.drift.tools import DriftRunContext, register_drift_tools
from kirakira_agent.events import OutboundMessage
from kirakira_agent.models.base import ModelClient
from kirakira_agent.proactive.config import DriftConfig
from kirakira_agent.session import SessionManager
from kirakira_agent.tools.builtins import build_default_registry

logger = logging.getLogger(__name__)


class DriftRunner:
    def __init__(
        self,
        *,
        config: DriftConfig,
        workspace: Path,
        bus: MessageBus,
        session_manager: SessionManager,
        model_client: ModelClient,
        model: str,
        memory: Any | None = None,
        target_channel: str = "",
        target_chat_id: str = "",
        max_tokens: int = 4000,
    ) -> None:
        self._cfg = config
        self._workspace = Path(workspace)
        self._bus = bus
        self._sessions = session_manager
        self._client = model_client
        self._model = model
        self._memory = memory
        self._channel = target_channel
        self._chat_id = target_chat_id
        self._max_tokens = max_tokens
        self._state = DriftStateStore(self._workspace / "drift" / "drift.db")

    def close(self) -> None:
        self._state.close()

    async def maybe_run(self, now: datetime, session_key: str) -> bool:
        """满足条件则跑一轮 Drift，返回是否真的跑了。"""
        if not self._cfg.enabled:
            return False
        ensure_example_skill(self._workspace)
        skills = discover_skills(self._workspace)
        if not skills:
            return False
        if not self._state.can_run(now, self._cfg.min_interval_hours):
            return False

        skill = self._select_skill(skills)
        ctx = DriftRunContext(skill=skill.name)
        briefing = self._build_briefing(skill, session_key)
        logger.info("[drift] 开始 skill=%s", skill.name)

        try:
            await asyncio.to_thread(self._run_agent, skill, briefing, ctx, session_key)
        except Exception:
            logger.exception("[drift] agent run 失败 skill=%s", skill.name)
            self._state.record_run(
                skill=skill.name,
                now=now,
                status="paused",
                briefing="run 异常中断",
                message_result="silent",
            )
            return True

        message_result = await self._commit(ctx, session_key)
        self._state.record_run(
            skill=skill.name,
            now=now,
            status=ctx.status if ctx.finished else "paused",
            briefing=ctx.briefing or "(未填写)",
            message_result=message_result,
        )
        if ctx.scratchpad_update or ctx.next_tendency:
            self._state.save_continuum(
                skill=skill.name,
                now=now,
                scratchpad=ctx.scratchpad_update,
                next_tendency=ctx.next_tendency,
            )
        logger.info(
            "[drift] 结束 skill=%s status=%s message=%s",
            skill.name,
            ctx.status,
            message_result,
        )
        return True

    def _select_skill(self, skills: List[DriftSkill]) -> DriftSkill:
        """每轮重新比较，选最久没跑过的 skill（从未跑过的优先）。

        排序键用 run_at 的 ISO 串：从未跑过 → "" 最小 → 最先选；跑过的按最早 run_at 优先。
        """
        last_run = self._state.last_run_at_by_skill()
        return min(
            skills,
            key=lambda s: last_run[s.name].isoformat() if s.name in last_run else "",
        )

    def _run_agent(
        self,
        skill: DriftSkill,
        briefing: str,
        ctx: DriftRunContext,
        session_key: str,
    ) -> None:
        """在工作线程里同步跑一次 agent run。"""
        registry = build_default_registry(
            self._workspace,
            memory=self._memory,
            session_manager=self._sessions,
            bus=self._bus,
        )
        register_drift_tools(registry, ctx)
        token = registry.set_context(
            channel=self._channel,
            chat_id=self._chat_id,
            session_key=session_key,
        )
        try:
            agent = Agent(
                model_client=self._client,
                tool_registry=registry,
                model=self._model,
                workdir=self._workspace,
                system=skill.body,
                max_tokens=self._max_tokens,
            )
            agent.run(
                [{"role": "user", "content": briefing}],
                max_rounds=self._cfg.max_steps,
            )
        finally:
            registry.reset_context(token)
            asyncio.run(registry.shutdown())

    async def _commit(self, ctx: DriftRunContext, session_key: str) -> str:
        """把 Drift 草稿消息投递出去（若有）。返回 sent / silent。"""
        if not (ctx.message_pushed and ctx.draft_message and self._channel and self._chat_id):
            return "silent"
        try:
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=self._channel,
                    chat_id=self._chat_id,
                    content=ctx.draft_message,
                    metadata={"proactive": True, "drift": True},
                )
            )
        except Exception:
            logger.exception("[drift] 投递草稿消息失败")
            return "silent"
        try:
            session = self._sessions.get_or_create(session_key)
            session.add_message("assistant", ctx.draft_message, proactive=True, drift=True)
            self._sessions.save(session)
        except Exception:
            logger.exception("[drift] 记录 Drift 消息失败")
        return "sent"

    def _build_briefing(self, skill: DriftSkill, session_key: str) -> str:
        """拼一份 Drift Briefing：记忆 + 近期上下文 + 本 skill 连续性 + 最近 run。"""
        sections: List[str] = [
            "你现在处于 Drift 空闲模式：没有需要主动推送的内容，利用这段时间按下面的"
            "技能指南（已作为 system prompt）执行一个后台小任务。执行结束前必须调用 finish_drift。",
        ]
        memory_text = self._read_memory()
        if memory_text:
            sections.append("【长期记忆】\n" + memory_text.strip()[:4000])
        recent_context = self._read_recent_context()
        if recent_context:
            sections.append("【近期上下文】\n" + recent_context.strip()[:2000])
        continuum = self._state.get_continuum(skill.name)
        if continuum.get("scratchpad") or continuum.get("next_tendency"):
            sections.append(
                "【本技能前情】\n"
                + f"scratchpad: {continuum.get('scratchpad') or '（无）'}\n"
                + f"上轮倾向: {continuum.get('next_tendency') or '（无）'}"
            )
        recent_runs = self._state.recent_runs(limit=5)
        if recent_runs:
            lines = [
                f"- {r['run_at']} {r['skill']} [{r['status']}] {r['briefing']}"
                for r in recent_runs
            ]
            sections.append("【最近 Drift 记录】\n" + "\n".join(lines))
        return "\n\n".join(sections)

    def _read_memory(self) -> str:
        reader = getattr(self._memory, "read_long_term", None)
        if not callable(reader):
            return ""
        try:
            return str(reader() or "")
        except Exception:
            return ""

    def _read_recent_context(self) -> str:
        reader = getattr(self._memory, "read_recent_context", None)
        if not callable(reader):
            return ""
        try:
            return str(reader() or "")
        except Exception:
            return ""
