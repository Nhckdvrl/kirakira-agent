"""Own conversation admission, persistence and post-commit memory."""

from __future__ import annotations

import logging
from dataclasses import asdict

from bus.events import OutboundMessage
from bus.events_lifecycle import (
    AfterReasoningResult,
    AfterTurnCtx,
    ContextBudgetUpdated,
    TurnCommitted,
    TurnState,
)
from core.memory.engine import MemoryCapability as _CoreMemCapability
from plugins.extensions.phases import _run_plugin_modules
from plugins.models.context_policy import estimate_context_tokens
from plugins.reply_program.program import current_program, unpublished_chain

logger = logging.getLogger(__name__)


def _engine_can_retrieve(engine: object) -> bool:
    """引擎是否具备真实上下文检索能力。DisabledMemoryEngine 能力集为空,走旧词法路径。"""
    descriptor = getattr(engine, "DESCRIPTOR", None)
    capabilities = getattr(descriptor, "capabilities", frozenset())
    return _CoreMemCapability.RETRIEVE_CONTEXT_BLOCK in capabilities


class DeliveryService:
    """Commit canonical output and program settlement together, then notify delivery."""

    def __init__(self, sessions, bus, event_bus, programs=None, *, jobs=None):
        self.session_manager, self.bus, self.event_bus = sessions, bus, event_bus
        self.programs = programs
        self.jobs = jobs

    async def complete(
        self, state: TurnState, result: AfterReasoningResult, *, after_turn_modules=()
    ) -> OutboundMessage:
        session = state.session
        msg = state.msg
        if msg.metadata.get("skip_post_memory") or msg.metadata.get("omit_user_turn"):
            state.extra_metadata["skip_post_memory"] = True
        provenance = {
            "source": str(msg.metadata.get("source") or "conversation"),
            "skip_post_memory": bool(state.extra_metadata.get("skip_post_memory")),
        }
        state.extra_metadata.update(provenance)
        committed_user = None
        binding = current_program(state.session_key)
        program_metadata = (
            {"program_id": binding.identity, "input_id": binding.value["input_id"]}
            if binding is not None
            else {}
        )
        if not msg.metadata.get("omit_user_turn"):
            committed_user = session.add_message(
                "user",
                msg.content,
                media=msg.media,
                inbound_timestamp=msg.timestamp.isoformat(),
                **provenance,
                **program_metadata,
                **(
                    {"id": str(msg.metadata["input_message_id"])}
                    if msg.metadata.get("input_message_id")
                    else {}
                ),
            )
        committed_assistant = session.add_message(
            "assistant",
            result.outbound.content,
            media=result.outbound.media,
            reasoning_content=result.outbound.thinking,
            tools_used=list(result.ctx.tools_used),
            tool_chain=unpublished_chain(
                result.ctx.tool_chain, binding.value["published_effects"]
            )
            if binding is not None
            else list(result.ctx.tool_chain),
            **state.extra_metadata,
            **program_metadata,
        )
        result.outbound.session_message_id = committed_assistant["id"]
        session.metadata.update(
            {
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "last_sender": msg.sender,
                "last_turn_at": msg.timestamp.isoformat(),
                "turn_count": int(session.metadata.get("turn_count") or 0) + 1,
                "tool_call_count": int(session.metadata.get("tool_call_count") or 0)
                + sum(len(group.get("calls") or []) for group in result.ctx.tool_chain),
            }
        )
        if msg.metadata.get("username"):
            session.metadata["username"] = str(msg.metadata["username"])
        context_trace = dict(state.extra_metadata.get("context_trace") or {})
        attempts = list(context_trace.get("attempts") or [])
        selected_plan = str(context_trace.get("selected_plan") or "")
        selected_attempt = next(
            (
                item
                for item in reversed(attempts)
                if str(item.get("name") or "") == selected_plan
            ),
            attempts[-1] if attempts else {},
        )
        history = session.get_history(max_messages=max(1, len(session.messages)))
        post_reply_budget = {
            "history_messages": len(history),
            "history_tokens_estimate": estimate_context_tokens(history, []),
            "estimate_quality": "approximate",
            "selected_plan": selected_plan,
            "last_prompt_tokens_estimate": int(
                selected_attempt.get("estimated_tokens") or 0
            ),
            "input_budget": int(selected_attempt.get("input_budget") or 0),
            "model_usage": dict(
                (context_trace.get("react_stats") or {}).get("model_usage") or {}
            ),
        }
        session.metadata["context_budget"] = post_reply_budget
        binding = current_program(state.session_key)
        if binding is not None:
            result.outbound.metadata["program_id"] = binding.identity

            def settle(tx):
                self.programs.store.settle(
                    binding.identity,
                    binding.lease,
                    "completed",
                    result=asdict(result.outbound),
                    transaction=tx,
                )
                if self.jobs is not None and state.extra_metadata.get(
                    "background_jobs_read"
                ):
                    self.jobs.finish(binding.identity, committed_assistant["id"], tx)

            self.session_manager.save(
                session,
                transaction_hook=settle,
            )
        else:
            self.session_manager.save(session)
        await self.event_bus.fanout(
            ContextBudgetUpdated(
                session_key=state.session_key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                **post_reply_budget,
            )
        )
        committed = TurnCommitted(
            session_key=state.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            user_content=msg.content,
            user_message_id=committed_user["id"] if committed_user else None,
            assistant_message_id=committed_assistant["id"],
            assistant_reply=result.outbound.content,
            tool_chain_raw=tuple(result.ctx.tool_chain),
            timestamp=msg.timestamp,
            extra={
                **state.extra_metadata,
                "skip_post_memory": bool(
                    msg.metadata.get("skip_post_memory")
                    or msg.metadata.get("omit_user_turn")
                    or state.extra_metadata.get("skip_post_memory")
                ),
            },
            tools_used=result.ctx.tools_used,
        )
        await self.event_bus.fanout(committed)
        after_turn = AfterTurnCtx(
            session_key=state.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            reply=result.outbound.content,
            tools_used=result.ctx.tools_used,
            thinking=result.outbound.thinking,
            will_dispatch=state.dispatch_outbound,
            extra_metadata=dict(state.extra_metadata),
        )
        await self.event_bus.fanout(after_turn)
        await _run_plugin_modules(after_turn_modules, after_turn)
        if state.dispatch_outbound:
            await self.bus.publish_outbound(result.outbound)
        return result.outbound
