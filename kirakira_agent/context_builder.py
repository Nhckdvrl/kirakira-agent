"""Prompt construction for passive turns."""

from __future__ import annotations

import platform
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.skills import SkillLoader


def _normalize_timestamp(ts: datetime | None) -> datetime:
    value = ts or datetime.now().astimezone()
    if value.tzinfo is None:
        value = value.astimezone()
    return value


def _weekday_cn(ts: datetime) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][ts.weekday()]


def build_time_envelope(ts: datetime | None) -> str:
    value = _normalize_timestamp(ts)
    yesterday = value - timedelta(days=1)
    tomorrow = value + timedelta(days=1)
    return (
        "[当前消息时间: %s | request_time=%s | 今天=%s（%s） | 昨天=%s（%s） | 明天=%s（%s） | 相对时间以此为准]"
        % (
            value.strftime("%Y-%m-%d %H:%M:%S %Z"),
            value.isoformat(),
            value.strftime("%Y-%m-%d"),
            _weekday_cn(value),
            yesterday.strftime("%Y-%m-%d"),
            _weekday_cn(yesterday),
            tomorrow.strftime("%Y-%m-%d"),
            _weekday_cn(tomorrow),
        )
    )


class ContextBuilder:
    def __init__(self, workspace: Path, memory: MemoryRuntime) -> None:
        self.workspace = workspace
        self.memory = memory
        self.skills = SkillLoader(workspace / "skills")

    def render(
        self,
        *,
        channel: str,
        chat_id: str,
        content: str,
        timestamp: datetime,
        history: List[Dict[str, Any]],
        retrieved_memory_block: str = "",
        skill_names: Optional[List[str]] = None,
        extra_hints: Optional[List[str]] = None,
        system_sections_top: Optional[List[str]] = None,
        system_sections_bottom: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        prompt = self._system_prompt(
            channel=channel,
            chat_id=chat_id,
            retrieved_memory_block=retrieved_memory_block,
            skill_names=skill_names or [],
            extra_hints=extra_hints or [],
            system_sections_top=system_sections_top or [],
            system_sections_bottom=system_sections_bottom or [],
        )
        user_text = build_time_envelope(timestamp) + "\n" + content
        return [
            {"role": "system", "content": prompt},
            *history,
            {"role": "user", "content": user_text},
        ]

    def _system_prompt(
        self,
        *,
        channel: str,
        chat_id: str,
        retrieved_memory_block: str,
        skill_names: List[str],
        extra_hints: List[str],
        system_sections_top: List[str],
        system_sections_bottom: List[str],
    ) -> str:
        workspace_path = str(self.workspace.resolve())
        sections = [
            "# Kirakira Agent",
            "你是一个可使用工具、拥有长期记忆、支持插件生命周期拦截的 AI agent。回答使用中文，短句、准确、必要时先调用工具。",
            "## 工作区\n- 根目录：%s\n- 长期记忆：%s/memory/MEMORY.md\n- 自我认知：%s/memory/SELF.md\n- 近期语境：%s/memory/RECENT_CONTEXT.md"
            % (workspace_path, workspace_path, workspace_path, workspace_path),
            "## 行为规范\n- 执行动作必须走工具；没有工具结果不得声称已完成。\n- 时间敏感、外部世界、版本、价格、新闻、状态类问题必须先核实。\n- 用户要求记住稳定偏好或事实时，调用 memorize。\n- 历史问题优先 recall_memory，必要时 search_messages 后 fetch_messages 回源。\n- 插件注入的上下文只作为系统候选上下文，不要复述其包装格式。",
            "## 环境\n%s" % platform.machine(),
            "## Current Session\nChannel: %s\nChat ID: %s" % (channel, chat_id),
            "## Long-Term Memory\n%s" % self.memory.store.read_long_term().strip(),
            "## Self Model\n%s" % self.memory.store.read_self().strip(),
            "## Recent Context\n%s" % self.memory.store.read_recent_context().strip(),
        ]
        if retrieved_memory_block.strip():
            sections.append(retrieved_memory_block.strip())
        if skill_names:
            loaded = [self.skills.load(name) for name in skill_names]
            sections.append("## Active Skills\n" + "\n\n".join(loaded))
        catalog = self.skills.descriptions()
        sections.append("## Skills\n" + catalog)
        if extra_hints:
            sections.append("## Turn Hints\n" + "\n".join("- " + h for h in extra_hints if h.strip()))
        return "\n\n---\n\n".join([*system_sections_top, *sections, *system_sections_bottom])

