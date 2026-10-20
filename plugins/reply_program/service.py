"""Own conversation admission, persistence and post-commit memory."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, List

from bus.event_bus import EventBus
from bus.events import InboundMessage, OutboundMessage
from bus.events_lifecycle import (
    AfterReasoningCtx,
    AfterReasoningResult,
    AfterStepCtx,
    BeforeReasoningCtx,
    BeforeTurnCtx,
    TurnFinished,
    TurnStarted,
    TurnState,
)
from bus.queue import MessageBus
from core.memory.engine import MemoryCapability as _CoreMemCapability
from core.memory.engine import MemoryQuery as _CoreMemQuery
from core.memory.engine import MemoryScope as _CoreMemScope
from core.memory.legacy import MemoryRuntime
from plugins.default_memory.contract import MemoryServices
from plugins.extensions.phases import LiveContributions, _run_plugin_modules
from plugins.ledger.compaction_runtime import SessionCompactionRuntime
from plugins.ledger.manager import SessionManager
from plugins.react.contract import ReactConfig
from plugins.react.reasoner import ReactProgram
from plugins.reply_program.program import current_program, unpublished_chain
from plugins.reply_program.store import unresolved
from plugins.retrieval.default_pipeline import RetrievalRequest
from plugins.tools.registry import ToolMenu, ToolRegistry

logger = logging.getLogger(__name__)


def _engine_can_retrieve(engine: object) -> bool:
    """引擎是否具备真实上下文检索能力。DisabledMemoryEngine 能力集为空,走旧词法路径。"""
    descriptor = getattr(engine, "DESCRIPTOR", None)
    capabilities = getattr(descriptor, "capabilities", frozenset())
    return _CoreMemCapability.RETRIEVE_CONTEXT_BLOCK in capabilities


class DefaultReplyProgram:
    def __init__(
        self,
        *,
        delivery,
        bus: MessageBus,
        event_bus: EventBus,
        session_manager: SessionManager,
        memory: MemoryRuntime,
        tools: ToolRegistry,
        reasoner: ReactProgram,
        config: ReactConfig,
        memory_services: "MemoryServices | None" = None,
        recall_inspector: Any = None,
        programs=None,
        jobs=None,
    ) -> None:
        self.delivery = delivery
        self.bus = bus
        self.event_bus = event_bus
        # 检索回放记录器;未注入时不记录,主链路行为不变。
        self.recall_inspector = recall_inspector
        self.session_manager = session_manager
        self.memory = memory
        # 记忆 DI 缝:runtime 只认识 MemoryServices.engine,不认识具体实现。
        self._memory_services = memory_services
        self.programs = programs
        self.jobs = jobs
        self.tools = tools
        self.reasoner = reasoner
        self.config = config
        # 在途 turn 持有各插件当前代际的租约,热重载只换代不抽走能力。
        self._before_turn_modules: List[object] = LiveContributions()
        self._before_reasoning_modules: List[object] = LiveContributions()
        self._after_reasoning_modules: List[object] = LiveContributions()
        self._after_turn_modules: List[object] = LiveContributions()
        self._active_sessions = set()
        self._turn_snapshots = {}
        self._step_subscription = self.event_bus.on(
            AfterStepCtx, self._capture_after_step
        )

    @property
    def memory_services(self):
        return self._memory_services

    def add_before_turn_plugin_modules(self, modules: List[object]) -> None:
        self._before_turn_modules.extend(modules)

    def add_before_reasoning_plugin_modules(self, modules: List[object]) -> None:
        self._before_reasoning_modules.extend(modules)

    def add_after_reasoning_plugin_modules(self, modules: List[object]) -> None:
        self._after_reasoning_modules.extend(modules)

    def add_after_turn_plugin_modules(self, modules: List[object]) -> None:
        self._after_turn_modules.extend(modules)

    async def execute(
        self, msg: InboundMessage, key: str, *, dispatch_outbound: bool = True
    ) -> OutboundMessage:
        """Execute one admitted conversation; capabilities are owned at invocation."""
        started_at = datetime.now().astimezone()
        started_clock = time.perf_counter()
        try:
            outbound = await self._run_turn(
                msg, key, dispatch_outbound=dispatch_outbound
            )
        except asyncio.CancelledError:
            await self._finish_turn(
                msg,
                key,
                status="interrupted",
                started_at=started_at,
                started_clock=started_clock,
                dispatch_outbound=dispatch_outbound,
            )
            raise
        except Exception as exc:
            await self._finish_turn(
                msg,
                key,
                status="error",
                started_at=started_at,
                started_clock=started_clock,
                dispatch_outbound=dispatch_outbound,
                error=str(exc),
            )
            raise
        await self._finish_turn(
            msg,
            key,
            status="success",
            started_at=started_at,
            started_clock=started_clock,
            dispatch_outbound=dispatch_outbound,
            outbound=outbound,
        )
        return outbound

    async def _finish_turn(
        self,
        msg: InboundMessage,
        key: str,
        *,
        status: str,
        started_at: datetime,
        started_clock: float,
        dispatch_outbound: bool,
        outbound: OutboundMessage | None = None,
        error: str = "",
    ) -> None:
        await self.event_bus.fanout(
            TurnFinished(
                session_key=key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                status=status,
                started_at=started_at,
                finished_at=datetime.now().astimezone(),
                duration_seconds=max(0.0, time.perf_counter() - started_clock),
                will_dispatch=dispatch_outbound,
                outbound=outbound,
                error=error,
            )
        )

    async def _run_turn(
        self, msg: InboundMessage, key: str, *, dispatch_outbound: bool = True
    ) -> OutboundMessage:
        metadata = (
            {"project_id": msg.metadata["project_id"]}
            if "project_id" in msg.metadata
            else None
        )
        session = self.session_manager.get_or_create(key, metadata=metadata)
        state = TurnState(
            msg=msg,
            session_key=key,
            dispatch_outbound=dispatch_outbound,
            session=session,
        )
        await self.event_bus.fanout(
            TurnStarted(
                session_key=key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                timestamp=msg.timestamp,
            )
        )
        before_turn: BeforeTurnCtx | None = None
        if msg.content.lstrip().startswith("/") and not str(
            msg.metadata.get("source") or ""
        ).startswith("scheduler:"):
            before_turn = BeforeTurnCtx(
                session_key=key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                timestamp=msg.timestamp,
                retrieved_memory_block="",
                history_messages=(),
                skill_names=self._collect_skill_mentions(msg.content),
            )
            before_turn = await self.event_bus.emit(before_turn)
            before_turn = await _run_plugin_modules(
                self._before_turn_modules, before_turn
            )
            state.extra_metadata.update(before_turn.extra_metadata)
            if before_turn.abort:
                return await self._dispatch_if_needed(state, before_turn.abort_reply)
            core_reply = self._core_command(msg.content)
            if core_reply is not None:
                return await self._dispatch_if_needed(state, core_reply)

        own_history = msg.metadata.get("history_scope") == "none"
        # A task's private request must not wait for or compact the whole chat.
        # Real persistence and admission continue to use the complete session.
        if not own_history:
            maintenance = self._markdown_maintenance()
            if maintenance is not None:
                await maintenance.wait_for_session(key)
            guard_reply = await self._guard_memory_context(session, key)
            if guard_reply:
                return await self._dispatch_if_needed(state, guard_reply)
        history = (
            []
            if own_history
            else session.get_history(max_messages=max(1, len(session.messages)))
        )
        binding = current_program(key)
        if binding is not None and binding.value["partial_ids"]:
            # Resume replaces only this program's prior interruption projection.
            # Its immutable rows remain in SessionDB and the raw request stays fixed.
            hidden = set(binding.value["partial_ids"])
            retained = [row for row in session.messages if row["id"] not in hidden]
            history = session._project_messages(retained)
        retrieved = ""
        if not msg.metadata.get("skip_memory_retrieval"):
            engine = (
                self.memory_services.engine
                if self.memory_services is not None
                else None
            )
            if engine is not None and _engine_can_retrieve(engine):
                # Phase 2:检索走 DI 服务包里的引擎(异步原生,无 to_thread)。
                result = await engine.query(
                    _CoreMemQuery(
                        text=msg.content,
                        intent="context",
                        scope=_CoreMemScope(
                            session_key=key,
                            channel=msg.context_channel,
                            chat_id=msg.context_chat_id,
                        ),
                        # timestamp 对 DefaultMemoryEngine 不承重,但 akasha 用它
                        # 算图激活的时间衰减——不传会直接返回 missing_query_timestamp。
                        # 照 Reference `agent/retrieval/default_pipeline.py` 一并传。
                        timestamp=msg.timestamp,
                    )
                )
                retrieved = result.text_block
                state.extra_metadata["retrieval_trace"] = {
                    "engine": result.trace.get("engine"),
                    "intent": "context",
                    "records": len(result.records),
                }
                # 检索回放:记下"召回了什么、注入了没有",供 Dashboard 逐轮回看。
                # 观测失败绝不能影响回复,所以整段吞异常。
                if self.recall_inspector is not None:
                    try:
                        self.recall_inspector.record_context_prepare(
                            session_key=key,
                            channel=msg.context_channel,
                            chat_id=msg.context_chat_id,
                            user_text=msg.content,
                            timestamp=msg.timestamp.isoformat(),
                            records=list(result.records),
                            text_block=retrieved,
                            trace=dict(result.trace or {}),
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning("[observe] 检索回放记录失败", exc_info=True)
            else:
                retrieval_result = await asyncio.to_thread(
                    self.memory.retrieve,
                    RetrievalRequest(
                        query=msg.content,
                        session_key=key,
                        channel=msg.context_channel,
                        chat_id=msg.context_chat_id,
                        history=session.get_history(
                            max_messages=max(1, len(session.messages))
                        ),
                        session_metadata=dict(session.metadata),
                        timestamp=msg.timestamp,
                    ),
                )
                retrieved = (
                    retrieval_result.block if retrieval_result is not None else ""
                )
                if retrieval_result is not None and retrieval_result.trace is not None:
                    trace = retrieval_result.trace
                    state.extra_metadata["retrieval_trace"] = {
                        "lanes": dict(trace.lanes),
                        "fused": trace.fused,
                        "injected": trace.injected,
                        "used_vector": trace.used_vector,
                        "truncated": trace.truncated,
                    }
        skill_mentions = self._collect_skill_mentions(msg.content)
        if before_turn is None:
            before_turn = BeforeTurnCtx(
                session_key=key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                timestamp=msg.timestamp,
                retrieved_memory_block=retrieved,
                history_messages=tuple(history),
                skill_names=skill_mentions,
            )
            before_turn = await self.event_bus.emit(before_turn)
            before_turn = await _run_plugin_modules(
                self._before_turn_modules, before_turn
            )
            state.extra_metadata.update(before_turn.extra_metadata)
            if before_turn.abort:
                return await self._dispatch_if_needed(state, before_turn.abort_reply)
        else:
            before_turn.history_messages = tuple(history)
            if retrieved:
                before_turn.retrieved_memory_block = "\n\n".join(
                    part
                    for part in (before_turn.retrieved_memory_block, retrieved)
                    if part
                )

        context_token = self.tools.set_context(
            session_key=key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            current_timestamp=msg.timestamp.isoformat(),
            timezone=str(
                msg.metadata.get("timezone")
                or session.metadata.get("timezone")
                or "UTC"
            ),
        )
        try:
            before_reasoning = BeforeReasoningCtx(
                session_key=key,
                channel=msg.context_channel,
                chat_id=msg.context_chat_id,
                content=msg.content,
                timestamp=msg.timestamp,
                skill_names=list(before_turn.skill_names),
                retrieved_memory_block=before_turn.retrieved_memory_block,
                extra_hints=list(before_turn.extra_hints),
            )
            before_reasoning = await self.event_bus.emit(before_reasoning)
            before_reasoning = await _run_plugin_modules(
                self._before_reasoning_modules, before_reasoning
            )
            if before_reasoning.abort:
                return await self._dispatch_if_needed(
                    state, before_reasoning.abort_reply
                )

            async def prepare_step():
                inputs = binding.prepare_inputs() if binding is not None else []
                if self.jobs is None or binding is None:
                    return inputs
                return inputs + [
                    self.jobs.material(fact)
                    for fact in self.jobs.claim(
                        key, binding.identity, binding.value["source"]
                    )
                ]

            turn = await self.reasoner.run_turn(
                msg=msg,
                session_key=key,
                history=list(before_turn.history_messages),
                retrieved_memory_block=before_reasoning.retrieved_memory_block,
                skill_names=before_reasoning.skill_names,
                extra_hints=before_reasoning.extra_hints,
                disabled_tools=self._disabled_tools(msg),
                session=None if own_history else session,
                compaction_runtime=None
                if own_history
                else SessionCompactionRuntime(self.session_manager),
                prepare_step=prepare_step,
            )
            state.extra_metadata["context_trace"] = dict(turn.context_trace)
            state.extra_metadata["execution_status"] = turn.stop_reason
            state.extra_metadata["background_jobs_read"] = bool(turn.execution_id)
        finally:
            self.tools.reset_context(context_token)
        # 入站 metadata 里的 mobile_attention 一律丢弃,只认本轮工具自己声明的
        # (照 Reference lifecycle/phases/after_reasoning.py:68)——否则渠道客户端
        # 可以伪造"需要确认"标记。
        inbound_metadata = dict(msg.metadata or {})
        inbound_metadata.pop("mobile_attention", None)
        after_ctx = AfterReasoningCtx(
            session_key=key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            tools_used=tuple(turn.tools_used),
            thinking=turn.thinking,
            tool_chain=tuple(turn.tool_chain),
            reply=turn.reply,
            # 照 Reference lifecycle/phases/after_reasoning.py:把本轮做了什么带出去。
            # 控制面据此投影 toolCall item,Web 端据此渲染工具时间线。
            outbound_metadata={
                **inbound_metadata,
                "tools_used": list(turn.tools_used),
                "tool_chain": list(turn.tool_chain),
                "execution_status": turn.stop_reason,
                **(
                    {"mobile_attention": turn.mobile_attention}
                    if turn.mobile_attention is not None
                    else {}
                ),
            },
        )
        after_ctx = await self.event_bus.emit(after_ctx)
        after_ctx = await _run_plugin_modules(self._after_reasoning_modules, after_ctx)
        outbound_metadata = dict(after_ctx.outbound_metadata)
        correlation_id = str(msg.metadata.get("client_request_id") or "").strip()
        if correlation_id:
            outbound_metadata["client_request_id"] = correlation_id
        outbound = OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=after_ctx.reply,
            thinking=after_ctx.thinking,
            media=list(after_ctx.media),
            metadata=outbound_metadata,
        )
        result = AfterReasoningResult(ctx=after_ctx, outbound=outbound)
        return await self._commit_and_dispatch(state, result)

    def _markdown_maintenance(self):
        """承重的 markdown 维护器;未接服务包时返回 None,回退旧 consolidation 路径。"""
        services = self.memory_services
        markdown = getattr(services, "markdown", None) if services is not None else None
        maintenance = getattr(markdown, "maintenance", None)
        # 未绑定 session 生命周期的维护器不能驱动归档。
        if maintenance is None or getattr(maintenance, "_get_session", None) is None:
            return None
        return maintenance

    async def _guard_memory_context(self, session: Any, session_key: str) -> str:
        total = len(session.messages)
        last = max(0, min(int(session.last_consolidated or 0), total))
        pending = total - last
        minimum_new = max(5, self.config.history_window // 2)
        threshold = self.config.history_window + minimum_new
        if pending < threshold:
            return ""
        maintenance = self._markdown_maintenance()
        if maintenance is None:
            # 没有记忆服务包 = 没有归档能力(最小构造/测试)。此时既无法推进也无从等待,
            # 拒绝每一轮只会让最小构造不可用,因此放行。生产始终有维护器,保护完整生效。
            return ""
        before = int(session.last_consolidated or 0)
        # 强制归档并等待:与队列维护共用同一把 session 锁,不会互相插队。
        from core.memory.markdown import ConsolidateRequest

        try:
            await maintenance.consolidate(
                ConsolidateRequest(
                    session=session,
                    force=True,
                    scope_channel=str(session.metadata.get("channel") or ""),
                    scope_chat_id=str(session.metadata.get("chat_id") or ""),
                )
            )
        except Exception:
            logger.exception("guard consolidation failed: %s", session_key)
        else:
            if int(session.last_consolidated or 0) > before:
                await self.session_manager.save_async(session)
        if int(session.last_consolidated or 0) > before:
            return ""
        return (
            "当前会话尚未归档的上下文已达到安全阈值，自动 consolidation 未能推进。"
            "为避免静默丢失历史，本轮已停止；请稍后重试或新建会话。"
        )

    async def _commit_and_dispatch(self, state, result):
        return await self.delivery.complete(
            state, result, after_turn_modules=self._after_turn_modules
        )

    async def _dispatch_if_needed(
        self, state: TurnState, content: str
    ) -> OutboundMessage:
        metadata = {}
        correlation_id = str(state.msg.metadata.get("client_request_id") or "").strip()
        if correlation_id:
            metadata["client_request_id"] = correlation_id
        outbound = OutboundMessage(
            channel=state.msg.channel,
            chat_id=state.msg.chat_id,
            content=content,
            metadata=metadata,
        )
        if state.dispatch_outbound:
            await self.bus.publish_outbound(outbound)
        return outbound

    def _collect_skill_mentions(self, content: str) -> List[str]:
        names = set(self.reasoner.context.skills.names())
        result: List[str] = []
        for name in re.findall(r"\$([a-zA-Z0-9_:-]+)", content):
            if name in names and name not in result:
                result.append(name)
        return result

    def _core_command(self, content: str) -> str | None:
        command = content.strip().lower()
        if command == "/tools":
            # MCP 工具只挂在快照上，要连同当前代际一起列出，否则用户会以为没接上。
            return "\n".join(ToolMenu(self.tools).names())
        if command == "/skills":
            self.reasoner.context.skills.reload()
            return self.reasoner.context.skills.descriptions()
        if command == "/memory":
            records = self.memory.list_records()
            return json.dumps(
                {"active_count": len(records), "recent": records[:10]},
                ensure_ascii=False,
                indent=2,
            )
        return None

    @staticmethod
    def _disabled_tools(msg: InboundMessage) -> set[str]:
        raw = msg.metadata.get("disabled_tools")
        if isinstance(raw, str):
            return {raw} if raw else set()
        if isinstance(raw, (list, tuple, set)):
            return {str(item) for item in raw if str(item)}
        return set()

    def _capture_after_step(self, event: AfterStepCtx) -> None:
        if event.session_key in self._active_sessions:
            self._turn_snapshots[event.session_key] = event

    def _persist_interrupted_turn(
        self,
        session_key: str,
        item: InboundMessage,
        *,
        binding=None,
        status="paused",
        error=None,
        recovery_pending=False,
    ) -> None:
        if item.metadata.get("omit_user_turn") and binding is None:
            return
        session = self.session_manager.get_or_create(session_key)
        if binding is not None:
            row = binding.snapshot()
            ids = list(row["partial_ids"])
            if not ids:
                if not item.metadata.get("omit_user_turn"):
                    user = session.add_message(
                        "user",
                        item.content,
                        media=item.media,
                        inbound_timestamp=item.timestamp.isoformat(),
                        program_id=binding.identity,
                        source=str(item.metadata.get("source") or "conversation"),
                        skip_post_memory=bool(item.metadata.get("skip_post_memory")),
                    )
                    ids.append(user["id"])
            ids.extend(identity for identity in self.programs.store.project_steers(session, binding) if identity not in ids)
            partial = session.add_message(
                "assistant",
                "[interrupted]",
                interrupted=True,
                input_update=status == "superseded",
                program_id=binding.identity,
                source=str(item.metadata.get("source") or "conversation"),
                skip_post_memory=True,
                tool_chain=unpublished_chain(
                    self.programs.chain(binding), row["published_effects"]
                ),
                unresolved_effects=list(unresolved(row)),
            )
            ids.append(partial["id"])
            self.session_manager.save(
                session,
                transaction_hook=lambda tx: self.programs.store.settle(
                    binding.identity,
                    binding.lease,
                    status,
                    error=error,
                    partial_ids=ids,
                    transaction=tx,
                    recovery_pending=recovery_pending
                    or (
                        self.programs.suspending
                        and status == "paused"
                        and row.get("auto_resume", False)
                    ),
                ),
            )
            return
        inbound_timestamp = item.timestamp.isoformat()
        if len(session.messages) >= 2:
            possible_user = session.messages[-2]
            possible_assistant = session.messages[-1]
            if (
                possible_user.get("role") == "user"
                and possible_user.get("inbound_timestamp") == inbound_timestamp
                and possible_assistant.get("role") == "assistant"
            ):
                self.session_manager.save(session)
                return
        session.add_message(
            "user",
            item.content,
            media=item.media,
            inbound_timestamp=inbound_timestamp,
            source=str(item.metadata.get("source") or "conversation"),
            skip_post_memory=bool(item.metadata.get("skip_post_memory")),
            **(
                {"id": str(item.metadata["input_message_id"])}
                if item.metadata.get("input_message_id")
                else {}
            ),
        )
        snapshot = self._turn_snapshots.get(session_key)
        session.add_message(
            "assistant",
            "[interrupted]",
            tools_used=list(snapshot.tools_used_so_far) if snapshot else [],
            tool_chain=list(snapshot.tool_chain_partial) if snapshot else [],
            reasoning_content=snapshot.partial_thinking if snapshot else "",
            partial_reply=snapshot.partial_reply if snapshot else "",
            interrupted=True,
            source=str(item.metadata.get("source") or "conversation"),
            skip_post_memory=bool(item.metadata.get("skip_post_memory")),
        )
        self.session_manager.save(session)

    def close(self):
        self._step_subscription.close()
