"""Build conversation context and retry only before tool side effects."""

from __future__ import annotations

import json
from typing import Any, List, Optional, Tuple

from bus.events import InboundMessage
from bus.events_lifecycle import ContextPrepared, PromptRenderCtx
from core.schema import JsonDict
from plugins.context import DEFAULT_CONTEXT_TRIM_PLANS, PromptSectionRender
from plugins.context.compaction import _CompactionProviderAdapter
from plugins.extensions.phases import _run_plugin_modules
from plugins.ledger.compaction_runtime import (
    CompactionProjection,
    SessionCompactionRuntime,
)
from plugins.models.context_compaction import (
    ContextPayloadSegments,
    hard_input_limit,
    window_initial_context_units,
)
from plugins.models.context_policy import estimate_context_tokens
from plugins.models.types import ContentSafetyError, ContextLengthError
from plugins.react.contract import ReactResult
from plugins.reply_program.program import current_program, replay_messages
from plugins.reply_program.store import tool_chain as program_tool_chain
from plugins.reply_program.store import unresolved
from plugins.tools.registry import ToolMenu


class ConversationProgram:
    def __init__(self, reasoner):
        self.reasoner = reasoner
        self.config = reasoner.config
        self.context = reasoner.context
        self.event_bus = reasoner.event_bus
        self.tools = reasoner.tools
        self._unlocked_tools = reasoner._unlocked_tools
        self._prompt_render_modules = reasoner._prompt_render_modules

    async def run(
        self,
        *,
        msg: InboundMessage,
        session_key: str,
        history: List[JsonDict],
        retrieved_memory_block: str,
        skill_names: Optional[List[str]],
        extra_hints: Optional[List[str]],
        disabled_tools: Optional[set[str]] = None,
        max_iterations_override: Optional[int] = None,
        session: Any | None = None,
        compaction_runtime: SessionCompactionRuntime | None = None,
    ) -> ReactResult:
        if self.context is None:
            raise RuntimeError(
                "ConversationProgram requires a context builder; use run for a scoped program"
            )
        provider_adapter = _CompactionProviderAdapter(self.reasoner)
        projection: CompactionProjection | None = None
        program = current_program(session_key)
        hidden = set(program.value["partial_ids"]) if program is not None else set()
        resumed_groups = (
            program_tool_chain(program.value) if program is not None else []
        )
        resumed_batches = tuple(
            tuple(replay_messages([group])) for group in resumed_groups
        )
        canonical_history = (
            session.get_history(max_messages=max(1, len(session.messages)))
            if session is not None
            else None
        )
        if (
            session is not None
            and compaction_runtime is not None
            and (list(history) == canonical_history or hidden)
        ):
            projection = compaction_runtime.projection(
                session, prefix=[], current_anchor=[], pending=[]
            )
            committed_units = projection.segments.committed_units
            if hidden:
                if projection.active is not None and hidden.intersection(
                    projection.active.source_message_ids
                ):
                    raise ValueError(
                        "program interruption rows already entered a Session summary"
                    )
                committed_units = tuple(
                    u
                    for u in committed_units
                    if not hidden.intersection(u.source_message_ids)
                )
            if projection.active is None and projection.head.next_generation == 1:
                committed_units = window_initial_context_units(
                    provider_adapter, committed_units
                )
            source_history = [
                *projection.segments.prefix,
                *[message for unit in committed_units for message in unit.messages],
            ]
            projection = CompactionProjection(
                segments=ContextPayloadSegments(
                    prefix=projection.segments.prefix,
                    committed_units=committed_units,
                    current_anchor=(),
                ),
                active=projection.active,
                head=projection.head,
            )
        else:
            source_history = list(history)
        attempts = self._build_attempt_plans(len(source_history))
        retry_trace: JsonDict = {"attempts": [], "selected_plan": None}
        tools = ToolMenu(self.tools)
        disabled = set(disabled_tools or ())
        unlocked = set(self._unlocked_tools.get(session_key, {}).keys())
        visible_specs = [
            spec for spec in tools.visible_specs(unlocked) if spec.name not in disabled
        ]
        for attempt, plan in enumerate(attempts):
            history_for_attempt = (
                list(source_history)
                if projection is not None
                else self._slice_history(source_history, int(plan["history_window"]))
            )
            render_ctx = PromptRenderCtx(
                session_key=session_key,
                channel=msg.context_channel,
                chat_id=msg.context_chat_id,
                content=msg.content,
                media=msg.media,
                timestamp=msg.timestamp,
                history=history_for_attempt,
                skill_names=skill_names,
                retrieved_memory_block=retrieved_memory_block,
                extra_hints=list(extra_hints or []),
                disabled_sections=set(plan["disabled_sections"]),
                turn_injection_prompt=self._build_deferred_tools_hint(tools, unlocked),
            )
            if program is not None and session is not None:
                unknown = {}
                for row in session.messages:
                    identity = row.get("program_id")
                    if identity and row.get("unresolved_effects"):
                        original = program.owner.store.read(identity, session_key)
                        unknown.update(unresolved(original))
                if unknown:
                    render_ctx.system_sections_bottom.append(
                        PromptSectionRender(
                            name="unknown_tool_outcomes",
                            is_static=False,
                            content="These earlier tool attempts have unknown external outcomes. Inspect their real state before repeating any corresponding effect. Proposed calls without a result are not evidence that nothing happened.\n"
                            + json.dumps(unknown, ensure_ascii=False),
                        )
                    )
            # Persisted control state is authoritative; the literal text
            # "[interrupted]" in a message alone must not create this instruction.
            if (
                session is not None
                and session.messages
                and session.messages[-1].get("role") == "assistant"
                and session.messages[-1].get("interrupted") is True
            ):
                render_ctx.system_sections_bottom.append(
                    PromptSectionRender(
                        name="turn_control",
                        content=(
                            (
                                "A new user input updated an unfinished task. Incorporate its corrections and constraints while continuing the original goal, unless the latest input clearly cancels or replaces it. "
                                if session.messages[-1].get("input_update") is True
                                else "The previous turn was interrupted and is closed. Follow the latest user request. Resume the earlier task only when the latest request explicitly asks to resume it. "
                            )
                            + "Previously completed tool effects remain real; an interruption does not roll them back. "
                            "Do not repeat effects whose outcome is uncertain without checking their state."
                        ),
                        is_static=False,
                    )
                )
            render_ctx = await self.event_bus.emit(render_ctx)
            render_ctx = await _run_plugin_modules(
                self._prompt_render_modules, render_ctx
            )
            rendered = self.context.render(
                channel=render_ctx.channel,
                chat_id=render_ctx.chat_id,
                content=render_ctx.content,
                media=render_ctx.media,
                timestamp=render_ctx.timestamp,
                history=render_ctx.history,
                retrieved_memory_block=render_ctx.retrieved_memory_block,
                skill_names=render_ctx.skill_names,
                extra_hints=render_ctx.extra_hints,
                system_sections_top=render_ctx.system_sections_top,
                system_sections_bottom=render_ctx.system_sections_bottom,
                disabled_sections=render_ctx.disabled_sections,
                turn_injection_prompt=render_ctx.turn_injection_prompt,
            )
            estimated = estimate_context_tokens(rendered.messages, visible_specs)
            input_budget = 0
            if self.config.context_window:
                input_budget = hard_input_limit(
                    provider_adapter, self.config.max_tokens
                )
            sections = tuple(
                {
                    "name": item.name,
                    "chars": item.chars,
                    "est_tokens": item.est_tokens,
                    "is_static": item.is_static,
                    "cache_hit": item.cache_hit,
                }
                for item in rendered.debug_breakdown
            )
            attempt_trace = {
                "name": plan["name"],
                "history_window": plan["history_window"],
                "history_messages": len(history_for_attempt),
                "disabled_sections": sorted(render_ctx.disabled_sections),
                "estimated_tokens": estimated,
                "estimate_quality": "approximate",
                "input_budget": input_budget,
                "sections": list(sections),
            }
            retry_trace["attempts"].append(attempt_trace)
            await self.event_bus.fanout(
                ContextPrepared(
                    session_key=session_key,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    attempt=attempt,
                    plan_name=str(plan["name"]),
                    history_messages=len(history_for_attempt),
                    disabled_sections=tuple(sorted(render_ctx.disabled_sections)),
                    estimated_tokens=estimated,
                    estimate_quality="approximate",
                    input_budget=input_budget,
                    context_frame_chars=len(rendered.context_frame),
                    sections=sections,
                )
            )
            rendered.messages.extend(
                message for batch in resumed_batches for message in batch
            )
            try:
                payload_segments: ContextPayloadSegments | None = None
                if projection is not None:
                    history_count = len(source_history)
                    payload_segments = ContextPayloadSegments(
                        prefix=(rendered.messages[0], *projection.segments.prefix),
                        committed_units=projection.segments.committed_units,
                        current_anchor=tuple(
                            rendered.messages[
                                1 + history_count : len(rendered.messages)
                                - sum(map(len, resumed_batches))
                            ]
                            if resumed_batches
                            else rendered.messages[1 + history_count :]
                        ),
                        active_batches=resumed_batches,
                    )
                result = await self.reasoner.run(
                    rendered.messages,
                    session_key=session_key,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    request_text=msg.content,
                    disabled_tools=disabled_tools,
                    max_iterations_override=max_iterations_override,
                    session=session,
                    compaction_runtime=compaction_runtime,
                    compaction_projection=projection,
                    payload_segments=payload_segments,
                )
            except (ContextLengthError, ContentSafetyError) as exc:
                attempt_trace["error"] = type(exc).__name__
                if attempt + 1 < len(attempts):
                    continue
                reply = (
                    "上下文过长，已尝试分级精简但仍无法处理；请新建会话或缩小请求。"
                    if isinstance(exc, ContextLengthError)
                    else "当前上下文触发了内容安全限制，无法继续处理。"
                )
                retry_trace["selected_plan"] = plan["name"]
                retry_trace["trimmed_sections"] = sorted(render_ctx.disabled_sections)
                return ReactResult(reply=reply, context_trace=retry_trace)
            retry_trace["selected_plan"] = plan["name"]
            retry_trace["trimmed_sections"] = sorted(render_ctx.disabled_sections)
            retry_trace["react_stats"] = dict(
                result.context_trace.get("react_stats") or {}
            )
            result.context_trace = retry_trace
            result.context_trace["execution_id"] = result.execution_id
            return result
        return ReactResult("上下文准备失败。", context_trace=retry_trace)

    @staticmethod
    def _slice_history(history: List[JsonDict], window: int) -> List[JsonDict]:
        if window <= 0:
            return []
        selected = list(history if window >= len(history) else history[-window:])
        first_user = next(
            (
                index
                for index, message in enumerate(selected)
                if message.get("role") == "user"
            ),
            None,
        )
        return selected[first_user:] if first_user is not None else []

    @staticmethod
    def _build_attempt_plans(total_history: int) -> List[JsonDict]:
        attempts: List[JsonDict] = []
        seen: set[Tuple[Tuple[str, ...], int]] = set()
        for trim_plan in DEFAULT_CONTEXT_TRIM_PLANS:
            disabled = set(trim_plan.drop_sections)
            key = (tuple(sorted(disabled)), total_history)
            if key not in seen:
                seen.add(key)
                attempts.append(
                    {
                        "name": trim_plan.name,
                        "disabled_sections": disabled,
                        "history_window": total_history,
                    }
                )
        return attempts

    @staticmethod
    def _build_deferred_tools_hint(tools: Any, unlocked: set[str]) -> str:
        names = [
            name
            for name in tools.names()
            if tools.is_deferred(name) and name not in unlocked
        ]
        if not names:
            return ""
        return (
            "【未加载工具目录（知道名字但 schema 未暴露）】\n"
            + ", ".join(names)
            + '\n加载方式：已知名字时调用 tool_search(query="select:工具名")；'
            '按能力查找时调用 tool_search(query="关键词")。'
        )
