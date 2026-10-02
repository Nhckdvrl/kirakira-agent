"""Canonical ReAct execution shared by conversation and scoped programs."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from agent.core.compaction import _CompactionProviderAdapter
from agent.core.contracts import ReasonerResult, RuntimeConfig
from agent.core.execution import ExecutionStore, execution_scope, policy_inputs
from agent.core.model_io import ModelInvoker
from agent.core.phases import _run_plugin_modules
from agent.core.tool_io import ToolInvoker
from agent.model_runtime.context_compaction import (
    ContextCompactionError,
    ContextCompactor,
    ContextPayloadSegments,
    PreparedQueryContext,
    compaction_scope_id,
)
from agent.model_runtime.context_policy import estimate_context_tokens
from agent.model_runtime.types import (
    ContentSafetyError,
    ContextLengthError,
    ModelClient,
)
from agent.model_runtime.usage import aggregate_usage, usage_from_mapping
from agent.plugins.snapshot import SnapshotToolView, get_current_runtime_snapshot
from agent.prompting import (
    PromptSectionRender,
    build_context_frame_content,
    build_context_frame_message,
)
from agent.prompting.context_builder import ContextBuilder
from agent.tool_hooks import ToolHook
from agent.tools.registry import ToolRegistry
from agent.turns.policy import ReasoningPolicy
from bus.event_bus import EventBus
from bus.events_lifecycle import AfterStepCtx, BeforeStepCtx
from core.schema import (
    ModelResponse,
    ToolResult,
    assistant_message_from_response,
    tool_result_message,
)
from session.compaction_runtime import CompactionProjection, SessionCompactionRuntime

logger = logging.getLogger(__name__)
JsonDict = Dict[str, Any]


class DefaultReasoner:
    def __init__(
        self,
        *,
        model_client: ModelClient,
        tools: ToolRegistry,
        config: RuntimeConfig,
        context: ContextBuilder | None,
        event_bus: EventBus,
        execution_store: ExecutionStore | None = None,
    ) -> None:
        self.model_client = model_client
        self.tools = tools
        self.config = config
        self.context = context
        self.event_bus = event_bus
        self.execution_store = execution_store
        self._model_io = ModelInvoker(model_client, config, event_bus)
        self._tool_io = ToolInvoker(tools, event_bus)
        self._prompt_render_modules: List[object] = []
        self._before_step_modules: List[object] = []
        self._after_step_modules: List[object] = []
        self._unlocked_tools: Dict[str, OrderedDict[str, None]] = {}

    def add_tool_hooks(self, hooks: List[ToolHook]) -> None:
        self._tool_io.add_hooks(hooks)

    def add_prompt_render_plugin_modules(self, modules: List[object]) -> None:
        self._prompt_render_modules.extend(modules)

    def add_before_step_plugin_modules(self, modules: List[object]) -> None:
        self._before_step_modules.extend(modules)

    def add_after_step_plugin_modules(self, modules: List[object]) -> None:
        self._after_step_modules.extend(modules)

    async def run(self, messages, **kwargs) -> ReasonerResult:
        policy = kwargs.get("policy") or ReasoningPolicy()
        if kwargs.get("disabled_tools"):
            names = set(
                SnapshotToolView(self.tools, get_current_runtime_snapshot()).names()
            )
            allowed = (
                names if policy.allowed_tools is None else set(policy.allowed_tools)
            )
            policy = replace(
                policy, allowed_tools=frozenset(allowed - set(kwargs["disabled_tools"]))
            )
        kwargs["policy"] = policy
        snapshot = get_current_runtime_snapshot()
        with execution_scope(
            self.execution_store,
            session_key=kwargs["session_key"],
            scope=kwargs.get("channel", "internal"),
            policy=policy,
            inputs={
                "messages": messages,
                "model": self.config.model,
                "max_steps": kwargs.get(
                    "max_iterations_override", self.config.max_iterations
                ),
                "policy": policy_inputs(policy),
                "snapshot_id": snapshot.snapshot_id if snapshot else None,
            },
        ) as execution:
            execution.check()
            try:
                result = await self._run(messages, **kwargs)
            except (ContextLengthError, ContentSafetyError) as exc:
                if execution.tool_started:
                    raise RuntimeError(
                        "Model rejected context after tool execution; refusing to replay side effects"
                    ) from exc
                raise
            execution.check()
            result.execution_id = execution.run_id
            result.context_trace["execution_id"] = execution.run_id
            if self.execution_store is not None:
                self.execution_store.finish(
                    execution.run_id, result.stop_reason, result=asdict(result)
                )
            return result

    async def _run(
        self,
        messages: List[JsonDict],
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        request_text: str,
        disabled_tools: Optional[set[str]] = None,
        max_iterations_override: Optional[int] = None,
        session: Any | None = None,
        compaction_runtime: SessionCompactionRuntime | None = None,
        compaction_projection: CompactionProjection | None = None,
        payload_segments: ContextPayloadSegments | None = None,
        policy: ReasoningPolicy | None = None,
    ) -> ReasonerResult:
        policy = policy or ReasoningPolicy()
        tools_used: List[str] = []
        tool_chain: List[JsonDict] = []
        final_reply = ""
        final_thinking = ""
        # 整轮只解析一次工具视图：本 turn 看到的 MCP 工具由 turn 开始时锁定的快照决定。
        tools = SnapshotToolView(self.tools, get_current_runtime_snapshot())
        disabled = set(disabled_tools or ())
        unlocked = set(self._unlocked_tools.get(session_key, {}).keys())
        repeated_calls: Dict[str, int] = {}
        react_input_samples: List[int] = []
        model_usages: List[JsonDict] = []
        empty_thinking_retry_used = False
        mobile_attention: Optional[str] = None
        iteration = 0
        iteration_limit = (
            self.config.max_iterations
            if max_iterations_override is None
            else max(1, int(max_iterations_override))
        )
        compaction_specs: List[Any] = []
        compactor: ContextCompactor | None = None
        compaction_head = compaction_projection.head if compaction_projection else None
        if (
            session is not None
            and compaction_runtime is not None
            and compaction_projection is not None
            and payload_segments is not None
        ):
            provider_adapter = _CompactionProviderAdapter(self)

            async def summarize_compaction(*, provider: Any, **request: Any) -> Any:
                summary_response = await self._complete_model(
                    list(request["messages"]),
                    [],
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    iteration=-1,
                )
                usage = usage_from_mapping(summary_response.usage)
                model_usages.append(dict(summary_response.usage or {}))
                return SimpleNamespace(
                    content=summary_response.text,
                    tool_calls=summary_response.tool_calls,
                    usage=usage,
                )

            compactor = ContextCompactor(
                provider=provider_adapter,
                model=self.config.model,
                scope_id=compaction_scope_id(session.key, session.created_at),
                active_compaction=compaction_projection.active,
                current_query=request_text,
                payload_segments=payload_segments,
                max_output_tokens=self.config.max_tokens,
                ledger_parent_generation=compaction_projection.head.parent_generation,
                next_generation=compaction_projection.head.next_generation,
                chat_call=summarize_compaction,
            )

        async def prepare_compaction(
            *, trigger: str = "soft_limit", force: bool = False
        ) -> PreparedQueryContext | None:
            nonlocal compaction_head
            if compactor is None:
                return None
            compactor.set_pending(messages)
            prepared = await compactor.prepare(
                messages,
                pending_start=compactor.pending_start,
                tools=compaction_specs,
                trigger=trigger,  # type: ignore[arg-type]
                force=force,
                max_output_tokens=self.config.max_tokens,
            )
            checkpoint = prepared.checkpoint
            if prepared.compacted and checkpoint is not None and checkpoint.committable:
                if compaction_head is None:
                    raise RuntimeError("compaction ledger head missing")
                row = compaction_runtime.commit_checkpoint(
                    session, checkpoint, head=compaction_head
                )
                compaction_head = type(compaction_head)(
                    session_key=session.key,
                    parent_generation=row.generation,
                    next_generation=row.generation + 1,
                )
                compactor.acknowledge_committed_checkpoint(row.generation)
            return prepared

        while iteration_limit <= 0 or iteration < iteration_limit:
            visible_specs = [
                spec
                for spec in tools.visible_specs(unlocked)
                if spec.name not in disabled and policy.allows(spec.name)
            ]
            visible_names = tuple(spec.name for spec in visible_specs)
            compaction_specs[:] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
                for spec in visible_specs
            ]
            await prepare_compaction()
            batch_start = len(messages)
            before_step = BeforeStepCtx(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                iteration=iteration,
                input_tokens_estimate=estimate_context_tokens(messages, visible_specs),
                visible_tool_names=visible_names,
            )
            before_step = await self.event_bus.emit(before_step)
            before_step = await _run_plugin_modules(
                self._before_step_modules, before_step
            )
            if before_step.early_stop:
                return ReasonerResult(
                    reply=before_step.early_stop_reply,
                    tools_used=tools_used,
                    tool_chain=tool_chain,
                    thinking=final_thinking,
                    mobile_attention=mobile_attention,
                )
            if before_step.extra_hints:
                messages.append(
                    build_context_frame_message(
                        build_context_frame_content(
                            [
                                PromptSectionRender(
                                    "plugin_hints",
                                    "\n".join(before_step.extra_hints),
                                    False,
                                )
                            ]
                        )
                    )
                )

            choice = policy.tool_choice
            if (
                policy.final_tool_choice is not None
                and iteration == iteration_limit - 1
            ):
                choice = policy.final_tool_choice
            model_options = {"tool_choice": choice} if choice is not None else {}
            react_input_samples.append(before_step.input_tokens_estimate)
            try:
                response = await self._complete_model(
                    messages,
                    visible_specs,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    iteration=iteration,
                    **model_options,
                )
            except ContextLengthError as overflow:
                pending_count = len(messages) - batch_start
                try:
                    compacted = await prepare_compaction(
                        trigger="context_overflow", force=True
                    )
                except ContextCompactionError:
                    raise overflow
                if compacted is None or not compacted.compacted:
                    raise overflow
                batch_start = len(messages) - pending_count
                response = await self._complete_model(
                    messages,
                    visible_specs,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    iteration=iteration,
                    **model_options,
                )
            model_usages.append(dict(response.usage or {}))
            final_reply = response.text or ""
            final_thinking = response.reasoning_content or final_thinking
            messages.append(assistant_message_from_response(response))
            if not response.tool_calls:
                if (
                    not final_reply.strip()
                    and response.reasoning_content
                    and not empty_thinking_retry_used
                ):
                    empty_thinking_retry_used = True
                    messages.append(
                        {
                            "role": "user",
                            "content": "<system-reminder>请输出给用户看的最终回复，不要只返回思考过程。</system-reminder>",
                        }
                    )
                    iteration += 1
                    continue
                await self._after_step(
                    session_key,
                    channel,
                    chat_id,
                    iteration,
                    messages,
                    (),
                    final_reply,
                    tools_used,
                    tool_chain,
                    final_thinking,
                    has_more=False,
                )
                self._remember_unlocked(session_key, unlocked)
                return ReasonerResult(
                    final_reply or "模型没有返回可展示的回复。",
                    tools_used,
                    tool_chain,
                    final_thinking,
                    context_trace={
                        "react_stats": self._react_stats(
                            react_input_samples, model_usages
                        )
                    },
                    mobile_attention=mobile_attention,
                )

            group = {
                "text": response.text or "",
                "reasoning_content": response.reasoning_content or "",
                "calls": [],
            }
            terminal_succeeded = False
            conflicting_batch = (
                policy.exclusive_terminal
                and len(response.tool_calls) != 1
                and any(
                    call.name in policy.terminal_tools for call in response.tool_calls
                )
            )
            for call in response.tool_calls:
                signature = "%s:%s" % (
                    call.name,
                    json.dumps(
                        call.arguments, ensure_ascii=False, sort_keys=True, default=str
                    ),
                )
                repeated_calls[signature] = repeated_calls.get(signature, 0) + 1
                if conflicting_batch or not policy.allows(call.name):
                    result = await self._deny_tool(
                        call,
                        session_key,
                        channel,
                        chat_id,
                        iteration,
                        "Error: tool batch violates this turn's execution policy",
                    )
                elif call.name in disabled:
                    result = await self._deny_tool(
                        call,
                        session_key,
                        channel,
                        chat_id,
                        iteration,
                        "Error: Tool '%s' is disabled for this turn" % call.name,
                    )
                elif tools.is_deferred(call.name) and call.name not in unlocked:
                    result = await self._deny_tool(
                        call,
                        session_key,
                        channel,
                        chat_id,
                        iteration,
                        "Error: Deferred tool '%s' is not loaded; call tool_search with select:%s"
                        % (call.name, call.name),
                    )
                elif repeated_calls[signature] > max(
                    1, self.config.repeated_tool_call_limit
                ):
                    result = await self._deny_tool(
                        call,
                        session_key,
                        channel,
                        chat_id,
                        iteration,
                        "Error: Repeated identical tool call blocked by loop guard",
                    )
                else:
                    result = await self._execute_tool(
                        call,
                        session_key,
                        channel,
                        chat_id,
                        request_text,
                        iteration,
                        tools,
                    )
                if call.name in policy.terminal_tools and result["status"] == "success":
                    terminal_succeeded = True
                tools_used.append(call.name)
                if result.get("mobile_attention") is not None:
                    mobile_attention = str(result["mobile_attention"])
                group["calls"].append(
                    {
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": result["arguments"],
                        "result": result["content"],
                        "status": result["status"],
                    }
                )
                messages.append(
                    tool_result_message(
                        ToolResult(
                            tool_call_id=call.id,
                            content=result["content"],
                            is_error=result["status"] != "success",
                        )
                    )
                )
                if call.name == "tool_search" and result["status"] == "success":
                    unlocked.update(self._unlocked_from_search(result["content"]))
                    self._remember_unlocked(session_key, unlocked)
                elif result["status"] == "success" and tools.is_deferred(call.name):
                    unlocked.add(call.name)
                    self._remember_unlocked(session_key, unlocked)
            tool_chain.append(group)
            if compactor is not None:
                compactor.record_completed_batch(messages, batch_start=batch_start)
            await self._after_step(
                session_key,
                channel,
                chat_id,
                iteration,
                messages,
                tuple(c.name for c in response.tool_calls),
                final_reply,
                tools_used,
                tool_chain,
                final_thinking,
                has_more=not terminal_succeeded,
            )
            if terminal_succeeded:
                return ReasonerResult(
                    final_reply,
                    tools_used,
                    tool_chain,
                    final_thinking,
                    context_trace={
                        "react_stats": self._react_stats(
                            react_input_samples, model_usages
                        )
                    },
                    mobile_attention=mobile_attention,
                    stop_reason="terminal_tool",
                )
            iteration += 1
        self._remember_unlocked(session_key, unlocked)
        summary_reply = ""
        if messages and policy.summarize_on_exhaustion:
            summary_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "<system-reminder>工具执行预算已结束。请基于已经获得的结果，"
                        "直接向用户给出阶段性回复：说明已完成什么、关键结果、尚缺什么和下一步。"
                        "不要继续调用工具，也不要暴露内部调用 ID。</system-reminder>"
                    ),
                },
            ]
            try:
                summary = await self._complete_model(
                    summary_messages,
                    [],
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    iteration=iteration,
                )
                summary_reply = summary.text.strip()
                final_thinking = summary.reasoning_content or final_thinking
                model_usages.append(dict(summary.usage or {}))
            except Exception:
                logger.exception("failed to generate tool-budget summary")
        return ReasonerResult(
            reply=summary_reply
            or final_reply
            or "工具执行预算已结束；已有结果已保留，可以在下一轮继续。",
            tools_used=tools_used,
            tool_chain=tool_chain,
            thinking=final_thinking,
            context_trace={
                "react_stats": self._react_stats(react_input_samples, model_usages)
            },
            mobile_attention=mobile_attention,
            stop_reason="budget_exhausted",
        )

    @staticmethod
    def _react_stats(input_samples: List[int], usages: List[JsonDict]) -> JsonDict:
        totals = aggregate_usage([usage_from_mapping(item) for item in usages])
        return {
            "iteration_count": len(input_samples),
            "input_token_estimates": list(input_samples),
            "max_input_tokens_estimate": max(input_samples, default=0),
            "model_usage": totals.to_dict(),
            # Backward-compatible shortcut used by the control-plane adapter.
            "request_count": totals.request_count,
        }

    @staticmethod
    def _unlocked_from_search(content: str) -> set[str]:
        try:
            payload = json.loads(content)
        except ValueError:
            return set()
        raw = payload.get("unlocked", []) if isinstance(payload, dict) else []
        return {str(item) for item in raw if isinstance(item, str) and item}

    def _remember_unlocked(self, session_key: str, names: set[str]) -> None:
        lru = self._unlocked_tools.setdefault(session_key, OrderedDict())
        for name in sorted(names):
            if name in lru:
                lru.move_to_end(name)
            else:
                lru[name] = None
            while len(lru) > 5:
                lru.popitem(last=False)

    async def _after_step(
        self,
        session_key: str,
        channel: str,
        chat_id: str,
        iteration: int,
        messages: List[JsonDict],
        tools_called: Tuple[str, ...],
        partial_reply: str,
        tools_used: List[str],
        tool_chain: List[JsonDict],
        thinking: str,
        *,
        has_more: bool,
    ) -> None:
        ctx = AfterStepCtx(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            iteration=iteration,
            context_tokens_estimate=max(
                1, len(json.dumps(messages, ensure_ascii=False)) // 3
            ),
            tools_called=tools_called,
            partial_reply=partial_reply,
            tools_used_so_far=tuple(tools_used),
            tool_chain_partial=tuple(tool_chain),
            partial_thinking=thinking,
            has_more=has_more,
        )
        await self.event_bus.fanout(ctx)
        await _run_plugin_modules(self._after_step_modules, ctx)

    async def run_turn(self, **kwargs) -> ReasonerResult:
        from agent.core.conversation import ConversationProgram

        return await ConversationProgram(self).run(**kwargs)

    async def _complete_model(self, *args, **kwargs) -> ModelResponse:
        return await self._model_io.complete(*args, **kwargs)

    async def _execute_tool(self, *args, **kwargs) -> JsonDict:
        return await self._tool_io.execute(*args, **kwargs)

    async def _deny_tool(self, *args, **kwargs) -> JsonDict:
        return await self._tool_io.deny(*args, **kwargs)
