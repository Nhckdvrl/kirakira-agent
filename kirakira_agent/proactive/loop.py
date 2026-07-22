"""主动推送主循环：Gate → Fetch → Ingest → Decide → Deliver →（空则）Drift。

参考 akashic 的 `proactive_v2/loop.py` + `plugins/wake_proactive/runtime.py`，
MVP 把重型的 phase-graph kernel / snapshot 压平成一个直白的 async tick 循环，
保留两条差异化本质：电量自适应调度 + 三通道语义 + 空闲交给 Drift。
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional

from kirakira_agent.bus import MessageBus
from kirakira_agent.events import OutboundMessage
from kirakira_agent.models.base import ModelClient
from kirakira_agent.proactive import energy
from kirakira_agent.proactive.config import ProactiveConfig
from kirakira_agent.proactive.contracts import (
    normalize_alert,
    normalize_content,
    normalize_context,
)
from kirakira_agent.proactive.judge import Decision, ProactiveJudge, format_context
from kirakira_agent.proactive.sources import SourceRegistry
from kirakira_agent.proactive.state import ProactiveStateStore
from kirakira_agent.session import SessionManager

logger = logging.getLogger(__name__)

# maybe_run(now, session_key) -> 是否真的跑了一轮 Drift
DriftHook = Callable[[datetime, str], Awaitable[bool]]

_PROACTIVE_CONTEXT_FILE = "PROACTIVE_CONTEXT.md"
_PROACTIVE_CONTEXT_TEMPLATE = """# Proactive Context

在这里写你对主动推送的明确规则，proactive 判断器每轮都会读取并遵守。
适合写白名单、黑名单、优先级、过滤条件。这里只定义规则，不提供内容事实。
"""


class ProactiveLoop:
    def __init__(
        self,
        *,
        config: ProactiveConfig,
        bus: MessageBus,
        session_manager: SessionManager,
        model_client: ModelClient,
        sources: SourceRegistry,
        state: ProactiveStateStore,
        memory: Any | None = None,
        drift_hook: DriftHook | None = None,
        passive_busy_fn: Callable[[str], bool] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._cfg = config
        self._bus = bus
        self._sessions = session_manager
        self._sources = sources
        self._state = state
        self._memory = memory
        self._drift_hook = drift_hook
        self._passive_busy_fn = passive_busy_fn
        self._rng = rng or random.Random()
        self._judge = ProactiveJudge(
            model_client,
            model=config.model,
            max_tokens=config.max_tokens,
        )
        self._running = False
        self._wake = asyncio.Event()
        self._workspace = Path(session_manager.workspace)

    # ── 生命周期 ──────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._ensure_context_file()
        logger.info(
            "[proactive] 已启动 target=%s:%s drift=%s",
            self._cfg.channel,
            self._cfg.chat_id,
            self._cfg.drift.enabled,
        )
        while self._running:
            interval = self._next_interval()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if not self._running:
                break
            try:
                await self._tick()
            except Exception:
                logger.exception("[proactive] tick 异常")

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def close(self) -> None:
        self._state.close()

    # ── 调度（电量模型）──────────────────────────────────────────

    def _next_interval(self) -> int:
        score = self._current_base_score()
        interval = energy.next_tick_from_score(
            score,
            tick_s1=self._cfg.tick_interval_s1,
            tick_s0=self._cfg.tick_interval_s0,
            tick_jitter=self._cfg.tick_jitter,
            rng=self._rng,
        )
        logger.info(
            "[proactive] 下次 tick 间隔=%ds base_score=%.3f", interval, score
        )
        return interval

    def _current_base_score(self) -> float:
        now = datetime.now(timezone.utc)
        last_user_at, recent_count = self._presence(now)
        e = energy.compute_energy(last_user_at, now)
        return energy.base_score(e, recent_count)

    # ── tick 主链路 ──────────────────────────────────────────────

    async def _tick(self) -> None:
        session_key = self._cfg.session_key
        now = datetime.now(timezone.utc)

        # 1. Gate：目标就绪、被动链路空闲
        if not self._cfg.target_ready:
            return
        if self._passive_busy_fn is not None and self._passive_busy_fn(session_key):
            logger.info("[proactive] 被动链路忙，跳过本轮")
            return

        # 2. Fetch + Ingest（三通道去重入库）
        channels = await self._sources.fetch_all()
        self._state.ingest("alert", channels["alert"], now)
        new_content = set(self._state.ingest("content", channels["content"], now))
        # context 不入库、不触发推送，只在本轮作为判断背景
        context_text = format_context(
            [normalize_context(item) for item in channels["context"]]
        )

        memory_text = self._read_memory()
        recent_conversation = self._recent_conversation(session_key)
        proactive_context = self._read_context_file()

        # 3. Decide：alert 优先直推
        alerts = self._state.unread("alert")
        if alerts:
            await self._push_alert(alerts[0], now, memory_text, recent_conversation,
                                   proactive_context, context_text)
            return

        # 4. content：只有出现新内容、且不在冷却期时才做兴趣判断
        contents = self._state.unread("content")
        has_new = bool(contents and new_content)
        if has_new and not self._state.in_cooldown(
            session_key, now, self._cfg.delivery_cooldown_hours
        ):
            pushed = await self._push_content(
                contents, now, memory_text, recent_conversation,
                proactive_context, context_text,
            )
            if pushed:
                return

        # 5. 三路都没推 → 交给 Drift 用空闲时间干活
        if self._drift_hook is not None:
            await self._drift_hook(now, session_key)

    async def _push_alert(
        self,
        alert_event: dict,
        now: datetime,
        memory_text: str,
        recent_conversation: str,
        proactive_context: str,
        context_text: str,
    ) -> None:
        contract = normalize_alert(alert_event)
        decision = await self._judge.decide_alert(
            contract,
            memory_text=memory_text,
            recent_conversation=recent_conversation,
            proactive_context=proactive_context,
            current_context=context_text,
        )
        await self._deliver(decision, now)
        source_id = str(alert_event.get("_source") or "")
        source_event_id = str(
            alert_event.get("event_id") or alert_event.get("id") or ""
        )
        if source_id and source_event_id:
            await self._sources.ack(source_id, [source_event_id])
        self._state.consume([contract.item_id], now)

    async def _push_content(
        self,
        content_events: List[dict],
        now: datetime,
        memory_text: str,
        recent_conversation: str,
        proactive_context: str,
        context_text: str,
    ) -> bool:
        page = content_events[: self._cfg.content_limit]
        contracts = [normalize_content(event) for event in page]
        decision = await self._judge.decide_content(
            contracts,
            memory_text=memory_text,
            recent_conversation=recent_conversation,
            proactive_context=proactive_context,
            current_context=context_text,
        )
        if not decision.send:
            return False
        await self._deliver(decision, now)
        # 只消费/ACK 被真正引用的内容
        cited = set(decision.cited_ids)
        selected = [c for c in contracts if c.item_id in cited]
        by_source: dict[str, List[str]] = {}
        for contract in selected:
            source_id = str(contract.raw.get("_source") or "")
            source_event_id = str(
                contract.raw.get("event_id") or contract.raw.get("id") or ""
            )
            if source_id and source_event_id:
                by_source.setdefault(source_id, []).append(source_event_id)
        for source_id, ids in by_source.items():
            await self._sources.ack(source_id, ids)
        self._state.consume([c.item_id for c in selected], now)
        return True

    # ── 交付 ────────────────────────────────────────────────────

    async def _deliver(self, decision: Decision, now: datetime) -> None:
        message = decision.message.strip()
        if not message:
            return
        await self._bus.publish_outbound(
            OutboundMessage(
                channel=self._cfg.channel,
                chat_id=self._cfg.chat_id,
                content=message,
                metadata={"proactive": True},
            )
        )
        self._state.mark_push(self._cfg.session_key, now)
        self._record_proactive_message(message)
        logger.info("[proactive] 已推送 message=%r", message[:120])

    def _record_proactive_message(self, message: str) -> None:
        """把主动消息落到目标 session，供后续 tick 感知近期已推内容，避免重复。"""
        try:
            session = self._sessions.get_or_create(self._cfg.session_key)
            session.add_message("assistant", message, proactive=True)
            self._sessions.save(session)
        except Exception:
            logger.exception("[proactive] 记录主动消息失败")

    # ── presence / 上下文读取 ────────────────────────────────────

    def _presence(self, now: datetime) -> tuple[Optional[datetime], int]:
        """从目标 session 推导 (最后一条用户消息时间, 近期用户+助手消息条数)。"""
        try:
            session = self._sessions.get_or_create(self._cfg.session_key)
        except Exception:
            return None, 0
        last_user_at: Optional[datetime] = None
        recent_count = 0
        window_start = now.timestamp() - 24 * 3600
        for msg in session.messages:
            ts = _parse_ts(msg.get("timestamp"))
            role = msg.get("role")
            if ts is not None and ts.timestamp() >= window_start:
                if role in ("user", "assistant"):
                    recent_count += 1
            if role == "user" and ts is not None:
                if last_user_at is None or ts > last_user_at:
                    last_user_at = ts
        return last_user_at, recent_count

    def _recent_conversation(self, session_key: str, limit: int = 20) -> str:
        try:
            session = self._sessions.get_or_create(session_key)
        except Exception:
            return ""
        lines: List[str] = []
        for msg in session.messages[-limit:]:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            if role == "assistant" and msg.get("proactive"):
                continue  # 主动消息不算被动对话
            content = str(msg.get("content") or "")[:300]
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)[:3000]

    def _read_memory(self) -> str:
        reader = getattr(self._memory, "read_long_term", None)
        if not callable(reader):
            return ""
        try:
            return str(reader() or "")
        except Exception:
            return ""

    def _context_file_path(self) -> Path:
        return self._workspace / _PROACTIVE_CONTEXT_FILE

    def _ensure_context_file(self) -> None:
        path = self._context_file_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_PROACTIVE_CONTEXT_TEMPLATE, encoding="utf-8")

    def _read_context_file(self) -> str:
        path = self._context_file_path()
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""


def _parse_ts(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt
