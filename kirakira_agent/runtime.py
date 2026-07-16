"""Akashic-style passive runtime: AgentLoop, PassiveTurnPipeline, and Reasoner."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kirakira_agent.bus import MessageBus
from kirakira_agent.context_builder import ContextBuilder
from kirakira_agent.context import microcompact
from kirakira_agent.event_bus import EventBus
from kirakira_agent.events import InboundMessage, OutboundMessage
from kirakira_agent.lifecycle import (
    AfterReasoningCtx,
    AfterReasoningResult,
    AfterStepCtx,
    AfterTurnCtx,
    BeforeReasoningCtx,
    BeforeStepCtx,
    BeforeTurnCtx,
    PromptRenderCtx,
    TurnCommitted,
    TurnStarted,
    ToolCallCompleted,
    ToolCallStarted,
    StreamDeltaReady,
    TurnState,
)
from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.models.base import ContentSafetyError, ContextLengthError, ModelClient
from kirakira_agent.schema import ModelResponse, ToolCall, ToolResult, assistant_message_from_response, tool_result_message
from kirakira_agent.session import SessionManager
from kirakira_agent.snapshot import (
    RuntimeSnapshotStore,
    SnapshotToolView,
    bind_runtime_snapshot,
    get_current_runtime_snapshot,
    reset_runtime_snapshot,
)
from kirakira_agent.tool_hooks import ToolExecutionRequest, ToolExecutor, ToolHook
from kirakira_agent.tools.registry import ToolRegistry
from kirakira_agent.channels.host import ChannelHost

logger = logging.getLogger(__name__)
JsonDict = Dict[str, Any]


@dataclass
class RuntimeConfig:
    model: str
    max_iterations: int = 10
    max_tokens: int = 8192
    history_window: int = 40
    model_timeout_seconds: float = 120.0
    repeated_tool_call_limit: int = 3
    stream: bool = True


@dataclass
class ReasonerResult:
    reply: str
    tools_used: List[str] = field(default_factory=list)
    tool_chain: List[JsonDict] = field(default_factory=list)
    thinking: str = ""


async def _run_plugin_modules(modules: List[object], ctx: Any) -> Any:
    current = ctx
    for module in modules:
        runner = getattr(module, "run", None)
        if runner is None and callable(module):
            runner = module
        if runner is None:
            continue
        result = runner(current)
        if inspect.isawaitable(result):
            result = await result
        if result is not None:
            current = result
    return current


class DefaultReasoner:
    def __init__(
        self,
        *,
        model_client: ModelClient,
        tools: ToolRegistry,
        config: RuntimeConfig,
        context: ContextBuilder,
        event_bus: EventBus,
    ) -> None:
        self.model_client = model_client
        self.tools = tools
        self.config = config
        self.context = context
        self.event_bus = event_bus
        self._tool_executor = ToolExecutor()
        self._prompt_render_modules: List[object] = []
        self._before_step_modules: List[object] = []
        self._after_step_modules: List[object] = []
        self._unlocked_tools: Dict[str, OrderedDict[str, None]] = {}

    def add_tool_hooks(self, hooks: List[ToolHook]) -> None:
        self._tool_executor.add_hooks(hooks)

    def add_prompt_render_plugin_modules(self, modules: List[object]) -> None:
        self._prompt_render_modules.extend(modules)

    def add_before_step_plugin_modules(self, modules: List[object]) -> None:
        self._before_step_modules.extend(modules)

    def add_after_step_plugin_modules(self, modules: List[object]) -> None:
        self._after_step_modules.extend(modules)

    async def run_turn(
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
    ) -> ReasonerResult:
        render_ctx = PromptRenderCtx(
            session_key=session_key,
            channel=msg.context_channel,
            chat_id=msg.context_chat_id,
            content=msg.content,
            media=msg.media,
            timestamp=msg.timestamp,
            history=history,
            skill_names=skill_names,
            retrieved_memory_block=retrieved_memory_block,
            extra_hints=list(extra_hints or []),
        )
        render_ctx = await self.event_bus.emit(render_ctx)
        render_ctx = await _run_plugin_modules(self._prompt_render_modules, render_ctx)
        messages = self.context.render(
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
        )
        return await self.run(
            messages,
            session_key=session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            request_text=msg.content,
            disabled_tools=disabled_tools,
            max_iterations_override=max_iterations_override,
        )

    async def run(
        self,
        messages: List[JsonDict],
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        request_text: str,
        disabled_tools: Optional[set[str]] = None,
        max_iterations_override: Optional[int] = None,
    ) -> ReasonerResult:
        tools_used: List[str] = []
        tool_chain: List[JsonDict] = []
        final_reply = ""
        final_thinking = ""
        # 整轮只解析一次工具视图：本 turn 看到的 MCP 工具由 turn 开始时锁定的快照决定。
        tools = SnapshotToolView(self.tools, get_current_runtime_snapshot())
        disabled = set(disabled_tools or ())
        unlocked = set(self._unlocked_tools.get(session_key, {}).keys())
        repeated_calls: Dict[str, int] = {}
        empty_thinking_retry_used = False
        iteration = 0
        iteration_limit = (
            self.config.max_iterations
            if max_iterations_override is None
            else max(1, int(max_iterations_override))
        )
        while iteration_limit <= 0 or iteration < iteration_limit:
            visible_specs = [
                spec
                for spec in tools.visible_specs(unlocked)
                if spec.name not in disabled
            ]
            visible_names = tuple(spec.name for spec in visible_specs)
            before_step = BeforeStepCtx(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                iteration=iteration,
                input_tokens_estimate=max(1, len(json.dumps(messages, ensure_ascii=False)) // 3),
                visible_tool_names=visible_names,
            )
            before_step = await self.event_bus.emit(before_step)
            before_step = await _run_plugin_modules(self._before_step_modules, before_step)
            if before_step.early_stop:
                return ReasonerResult(
                    reply=before_step.early_stop_reply,
                    tools_used=tools_used,
                    tool_chain=tool_chain,
                    thinking=final_thinking,
                )
            if before_step.extra_hints:
                messages.append(
                    {
                        "role": "user",
                        "content": "<system-reminder>%s</system-reminder>"
                        % "\n".join(before_step.extra_hints),
                    }
                )

            response = None
            for retry in range(3):
                try:
                    response = await self._complete_model(
                        messages,
                        visible_specs,
                        session_key=session_key,
                        channel=channel,
                        chat_id=chat_id,
                        iteration=iteration,
                    )
                    break
                except ContextLengthError:
                    if retry >= 2:
                        raise
                    messages = self._trim_context(messages, retry)
                except ContentSafetyError:
                    if retry >= 2:
                        raise
                    messages = self._trim_context(messages, retry + 1)
            if response is None:
                raise RuntimeError("Model did not return a response")
            final_reply = response.text or ""
            final_thinking = response.reasoning_content or final_thinking
            messages.append(assistant_message_from_response(response))
            if not response.tool_calls:
                if not final_reply.strip() and response.reasoning_content and not empty_thinking_retry_used:
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
                    session_key, channel, chat_id, iteration, messages, (), final_reply,
                    tools_used, tool_chain, final_thinking, has_more=False,
                )
                self._remember_unlocked(session_key, unlocked)
                return ReasonerResult(
                    final_reply or "模型没有返回可展示的回复。",
                    tools_used,
                    tool_chain,
                    final_thinking,
                )

            group = {
                "text": response.text or "",
                "reasoning_content": response.reasoning_content or "",
                "calls": [],
            }
            for call in response.tool_calls:
                signature = "%s:%s" % (
                    call.name,
                    json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str),
                )
                repeated_calls[signature] = repeated_calls.get(signature, 0) + 1
                if call.name in disabled:
                    result = await self._deny_tool(
                        call,
                        session_key,
                        channel,
                        chat_id,
                        "Error: Tool '%s' is disabled for this turn" % call.name,
                    )
                elif tools.is_deferred(call.name) and call.name not in unlocked:
                    result = await self._deny_tool(
                        call,
                        session_key,
                        channel,
                        chat_id,
                        "Error: Deferred tool '%s' is not loaded; call tool_search with select:%s"
                        % (call.name, call.name),
                    )
                elif repeated_calls[signature] > max(1, self.config.repeated_tool_call_limit):
                    result = await self._deny_tool(
                        call,
                        session_key,
                        channel,
                        chat_id,
                        "Error: Repeated identical tool call blocked by loop guard",
                    )
                else:
                    result = await self._execute_tool(
                        call, session_key, channel, chat_id, request_text, tools
                    )
                tools_used.append(call.name)
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
                has_more=True,
            )
            iteration += 1
        self._remember_unlocked(session_key, unlocked)
        summary_reply = ""
        if messages:
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
            except Exception:
                logger.exception("failed to generate tool-budget summary")
        return ReasonerResult(
            reply=summary_reply
            or final_reply
            or "工具执行预算已结束；已有结果已保留，可以在下一轮继续。",
            tools_used=tools_used,
            tool_chain=tool_chain,
            thinking=final_thinking,
        )

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

    @staticmethod
    def _trim_context(messages: List[JsonDict], level: int) -> List[JsonDict]:
        trimmed = [dict(message) for message in messages]
        microcompact(trimmed, keep_tool_results=1)
        if level <= 0:
            return trimmed
        system = [message for message in trimmed if message.get("role") == "system"]
        conversation = [
            message for message in trimmed if message.get("role") != "system"
        ]
        keep = max(4, len(conversation) // (2 if level == 1 else 4))
        tail = conversation[-keep:]
        first_user = next(
            (
                index
                for index, message in enumerate(tail)
                if message.get("role") == "user"
            ),
            None,
        )
        if first_user is not None:
            tail = tail[first_user:]
        if level >= 2:
            for message in system:
                content = str(message.get("content") or "")
                if len(content) > 16000:
                    message["content"] = (
                        content[:8000]
                        + "\n\n[system context trimmed after provider overflow]\n\n"
                        + content[-8000:]
                    )
        return [*system, *tail]

    async def _complete_model(
        self,
        messages: List[JsonDict],
        specs: List[Any],
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        iteration: int,
    ) -> ModelResponse:
        timeout = max(1.0, float(self.config.model_timeout_seconds))
        stream_method = getattr(self.model_client, "complete_stream", None)
        try:
            if not self.config.stream or not callable(stream_method):
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self.model_client.complete,
                        messages,
                        specs,
                        "",
                        self.config.model,
                        self.config.max_tokens,
                    ),
                    timeout=timeout,
                )
            queue: asyncio.Queue[Tuple[str, str]] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def on_delta(content: str, reasoning: str) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, (content, reasoning))

            worker = asyncio.create_task(
                asyncio.to_thread(
                    stream_method,
                    messages,
                    specs,
                    "",
                    self.config.model,
                    self.config.max_tokens,
                    on_delta,
                )
            )
            deadline = loop.time() + timeout
            while not worker.done() or not queue.empty():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    worker.cancel()
                    raise asyncio.TimeoutError
                try:
                    content_delta, reasoning_delta = await asyncio.wait_for(
                        queue.get(), timeout=min(0.1, remaining)
                    )
                except asyncio.TimeoutError:
                    continue
                await self.event_bus.fanout(
                    StreamDeltaReady(
                        session_key=session_key,
                        channel=channel,
                        chat_id=chat_id,
                        iteration=iteration,
                        content_delta=content_delta,
                        reasoning_delta=reasoning_delta,
                    )
                )
            return await worker
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "LLM 请求超过 %.1f 秒，已停止本轮。" % timeout
            ) from exc

    async def _execute_tool(
        self,
        call: ToolCall,
        session_key: str,
        channel: str,
        chat_id: str,
        request_text: str,
        tools: Any = None,
    ) -> JsonDict:
        request = ToolExecutionRequest(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            tool_name=call.name,
            arguments=call.arguments,
            call_id=call.id,
            request_text=request_text,
        )
        await self.event_bus.fanout(
            ToolCallStarted(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                call_id=call.id,
                tool_name=call.name,
                arguments=dict(call.arguments),
            )
        )

        registry = tools if tools is not None else self.tools

        async def invoke(tool_name: str, arguments: JsonDict) -> ToolResult:
            return await registry.execute_async(ToolCall(call.id, tool_name, arguments))

        result = await self._tool_executor.execute(request, invoke)
        content = result.output
        if result.extra_messages:
            content += "\n\n" + "\n".join(result.extra_messages)
        await self.event_bus.fanout(
            ToolCallCompleted(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                call_id=call.id,
                tool_name=call.name,
                arguments=dict(result.final_arguments),
                result=content,
                status=result.status,
            )
        )
        return {"content": content, "status": result.status, "arguments": result.final_arguments}

    async def _deny_tool(
        self,
        call: ToolCall,
        session_key: str,
        channel: str,
        chat_id: str,
        reason: str,
    ) -> JsonDict:
        started = ToolCallStarted(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            call_id=call.id,
            tool_name=call.name,
            arguments=dict(call.arguments),
        )
        await self.event_bus.fanout(started)
        await self.event_bus.fanout(
            ToolCallCompleted(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                call_id=call.id,
                tool_name=call.name,
                arguments=dict(call.arguments),
                result=reason,
                status="denied",
            )
        )
        return {
            "content": reason,
            "status": "denied",
            "arguments": dict(call.arguments),
        }

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
            context_tokens_estimate=max(1, len(json.dumps(messages, ensure_ascii=False)) // 3),
            tools_called=tools_called,
            partial_reply=partial_reply,
            tools_used_so_far=tuple(tools_used),
            tool_chain_partial=tuple(tool_chain),
            partial_thinking=thinking,
            has_more=has_more,
        )
        await self.event_bus.fanout(ctx)
        await _run_plugin_modules(self._after_step_modules, ctx)


class PassiveTurnPipeline:
    def __init__(
        self,
        *,
        bus: MessageBus,
        event_bus: EventBus,
        session_manager: SessionManager,
        memory: MemoryRuntime,
        tools: ToolRegistry,
        reasoner: DefaultReasoner,
        config: RuntimeConfig,
        snapshot_store: RuntimeSnapshotStore | None = None,
    ) -> None:
        self.bus = bus
        self.event_bus = event_bus
        self.session_manager = session_manager
        self.memory = memory
        self.tools = tools
        self.reasoner = reasoner
        self.config = config
        self.snapshot_store = snapshot_store
        self._before_turn_modules: List[object] = []
        self._before_reasoning_modules: List[object] = []
        self._after_reasoning_modules: List[object] = []
        self._after_turn_modules: List[object] = []

    def add_before_turn_plugin_modules(self, modules: List[object]) -> None:
        self._before_turn_modules.extend(modules)

    def add_before_reasoning_plugin_modules(self, modules: List[object]) -> None:
        self._before_reasoning_modules.extend(modules)

    def add_after_reasoning_plugin_modules(self, modules: List[object]) -> None:
        self._after_reasoning_modules.extend(modules)

    def add_after_turn_plugin_modules(self, modules: List[object]) -> None:
        self._after_turn_modules.extend(modules)

    async def run(self, msg: InboundMessage, key: str, *, dispatch_outbound: bool = True) -> OutboundMessage:
        """整个 turn 锁定一份能力快照，热重载不会在 turn 中途抽走工具。"""

        if self.snapshot_store is None or self.snapshot_store.current is None:
            return await self._run_turn(msg, key, dispatch_outbound=dispatch_outbound)
        lease = self.snapshot_store.lease()
        token = bind_runtime_snapshot(lease)
        try:
            return await self._run_turn(msg, key, dispatch_outbound=dispatch_outbound)
        finally:
            reset_runtime_snapshot(token)
            await lease.release()

    async def _run_turn(self, msg: InboundMessage, key: str, *, dispatch_outbound: bool = True) -> OutboundMessage:
        session = self.session_manager.get_or_create(key)
        state = TurnState(msg=msg, session_key=key, dispatch_outbound=dispatch_outbound, session=session)
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
        if msg.content.lstrip().startswith("/"):
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

        await self.memory.wait_for_session(key)
        history = session.get_history(max_messages=self.config.history_window)
        retrieved = (
            ""
            if msg.metadata.get("skip_memory_retrieval")
            else await asyncio.to_thread(self.memory.build_retrieval_block, msg.content)
        )
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
            before_reasoning = await _run_plugin_modules(self._before_reasoning_modules, before_reasoning)
            if before_reasoning.abort:
                return await self._dispatch_if_needed(state, before_reasoning.abort_reply)

            turn = await self.reasoner.run_turn(
                msg=msg,
                session_key=key,
                history=list(before_turn.history_messages),
                retrieved_memory_block=before_reasoning.retrieved_memory_block,
                skill_names=before_reasoning.skill_names,
                extra_hints=before_reasoning.extra_hints,
                disabled_tools=self._disabled_tools(msg),
            )
        finally:
            self.tools.reset_context(context_token)
        after_ctx = AfterReasoningCtx(
            session_key=key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            tools_used=tuple(turn.tools_used),
            thinking=turn.thinking,
            tool_chain=tuple(turn.tool_chain),
            reply=turn.reply,
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

    async def _commit_and_dispatch(self, state: TurnState, result: AfterReasoningResult) -> OutboundMessage:
        session = state.session
        msg = state.msg
        if not msg.metadata.get("omit_user_turn"):
            session.add_message(
                "user",
                msg.content,
                media=msg.media,
                inbound_timestamp=msg.timestamp.isoformat(),
            )
        session.add_message(
            "assistant",
            result.outbound.content,
            media=result.outbound.media,
            reasoning_content=result.outbound.thinking,
            tools_used=list(result.ctx.tools_used),
            tool_chain=list(result.ctx.tool_chain),
            **state.extra_metadata,
        )
        session.metadata.update(
            {
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "last_sender": msg.sender,
                "last_turn_at": msg.timestamp.isoformat(),
                "turn_count": int(session.metadata.get("turn_count") or 0) + 1,
                "tool_call_count": int(
                    session.metadata.get("tool_call_count") or 0
                )
                + sum(
                    len(group.get("calls") or [])
                    for group in result.ctx.tool_chain
                ),
            }
        )
        if msg.metadata.get("username"):
            session.metadata["username"] = str(msg.metadata["username"])
        if not msg.metadata.get("skip_post_memory"):
            await asyncio.to_thread(
                self.memory.consolidate_turn,
                session,
                msg.content,
                result.outbound.content,
            )
        self.session_manager.save(session)
        committed = TurnCommitted(
            session_key=state.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            user_content=msg.content,
            assistant_reply=result.outbound.content,
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
        await _run_plugin_modules(self._after_turn_modules, after_turn)
        if state.dispatch_outbound:
            await self.bus.publish_outbound(result.outbound)
        if not msg.metadata.get("skip_post_memory"):
            self.memory.schedule_consolidation(
                session,
                model_client=self.reasoner.model_client,
                model=self.config.model,
            )
        return result.outbound

    async def _dispatch_if_needed(self, state: TurnState, content: str) -> OutboundMessage:
        metadata = {}
        correlation_id = str(
            state.msg.metadata.get("client_request_id") or ""
        ).strip()
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
            return "\n".join(self.tools.names())
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


class AgentLoop:
    def __init__(self, *, bus: MessageBus, pipeline: PassiveTurnPipeline) -> None:
        self.bus = bus
        self.pipeline = pipeline
        self._running = False
        self._active_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._turn_snapshots: Dict[str, AfterStepCtx] = {}
        event_bus = getattr(self.pipeline, "event_bus", None)
        if event_bus is not None:
            event_bus.on(AfterStepCtx, self._capture_after_step)

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                item = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            key = item.session_key
            task = asyncio.create_task(
                self._process_item(item, key), name="turn:%s" % key
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _process_item(self, item: InboundMessage, key: str) -> None:
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                task = asyncio.current_task()
                if task is not None:
                    self._active_tasks[key] = task
                try:
                    await self.pipeline.run(item, key)
                except asyncio.CancelledError:
                    logger.info("turn cancelled for session=%s", key)
                    self._persist_interrupted_turn(key, item)
                    raise
                except Exception as exc:
                    logger.exception("failed to process inbound")
                    metadata = {}
                    correlation_id = str(
                        item.metadata.get("client_request_id") or ""
                    ).strip()
                    if correlation_id:
                        metadata["client_request_id"] = correlation_id
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            item.channel,
                            item.chat_id,
                            "出错：%s" % exc,
                            metadata=metadata,
                        )
                    )
                finally:
                    if self._active_tasks.get(key) is task:
                        self._active_tasks.pop(key, None)
                    self._turn_snapshots.pop(key, None)
        finally:
            await self.bus.complete_inbound(item)
            current = asyncio.current_task()
            if not lock.locked() and not any(
                task is not current
                and not task.done()
                and task.get_name() == "turn:%s" % key
                for task in self._tasks
            ):
                self._session_locks.pop(key, None)

    def stop(self) -> None:
        self._running = False

    def request_interrupt(self, session_key: str) -> bool:
        task = self._active_tasks.get(session_key)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self) -> None:
        self.stop()
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _capture_after_step(self, event: AfterStepCtx) -> None:
        self._turn_snapshots[event.session_key] = event

    def _persist_interrupted_turn(
        self, session_key: str, item: InboundMessage
    ) -> None:
        if item.metadata.get("omit_user_turn"):
            return
        session = self.pipeline.session_manager.get_or_create(session_key)
        inbound_timestamp = item.timestamp.isoformat()
        if len(session.messages) >= 2:
            possible_user = session.messages[-2]
            possible_assistant = session.messages[-1]
            if (
                possible_user.get("role") == "user"
                and possible_user.get("inbound_timestamp") == inbound_timestamp
                and possible_assistant.get("role") == "assistant"
            ):
                self.pipeline.session_manager.save(session)
                return
        session.add_message(
            "user",
            item.content,
            media=item.media,
            inbound_timestamp=inbound_timestamp,
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
        )
        self.pipeline.session_manager.save(session)


@dataclass
class CoreRuntime:
    bus: MessageBus
    event_bus: EventBus
    session_manager: SessionManager
    memory: MemoryRuntime
    tools: ToolRegistry
    context: ContextBuilder
    reasoner: DefaultReasoner
    pipeline: PassiveTurnPipeline
    loop: AgentLoop
    channel_host: ChannelHost | None = None
    plugin_manager: Any | None = None
    mcp_watcher: Any | None = None
    scheduler: Any | None = None
    subagents: Any | None = None

    def add_tool_hooks(self, hooks: List[ToolHook]) -> None:
        self.reasoner.add_tool_hooks(hooks)

    async def process_direct(
        self,
        content: str,
        *,
        session_key: str = "direct:local",
        channel: str = "direct",
        chat_id: str = "local",
        omit_user_turn: bool = False,
        skip_post_memory: bool = False,
        skip_memory_retrieval: bool = False,
        disabled_tools: List[str] | None = None,
    ) -> OutboundMessage:
        metadata: JsonDict = {
            "session_key_override": session_key,
            "omit_user_turn": omit_user_turn,
            "skip_post_memory": skip_post_memory,
            "skip_memory_retrieval": skip_memory_retrieval,
        }
        if disabled_tools:
            metadata["disabled_tools"] = list(disabled_tools)
        return await self.pipeline.run(
            InboundMessage(
                channel=channel,
                sender="direct_user",
                chat_id=chat_id,
                content=content,
                metadata=metadata,
            ),
            session_key,
            dispatch_outbound=False,
        )

    async def start_background(
        self, *, start_channels: bool = True
    ) -> list[asyncio.Task[Any]]:
        tasks = [
            asyncio.create_task(self.loop.run(), name="agent_loop"),
            asyncio.create_task(self.bus.dispatch_outbound(), name="bus_dispatch"),
        ]
        if start_channels and self.channel_host is not None:
            await self.channel_host.start_all()
        if self.scheduler is not None:
            tasks.append(asyncio.create_task(self.scheduler.run(), name="scheduler"))
        if self.mcp_watcher is not None:
            tasks.append(
                asyncio.create_task(self.mcp_watcher.run(), name="workspace_mcp_watcher")
            )
        return tasks

    async def stop_background(self, tasks: list[asyncio.Task[Any]]) -> None:
        if self.subagents is not None:
            await self.subagents.shutdown()
        await self.loop.shutdown()
        if self.scheduler is not None:
            self.scheduler.stop()
        if self.mcp_watcher is not None:
            self.mcp_watcher.stop()
        drained = await self.bus.drain(timeout=10.0)
        if not drained:
            logger.warning("outbound queue did not drain before shutdown")
        self.bus.stop()
        await self.bus.shutdown()
        if self.channel_host is not None:
            await self.channel_host.stop_all()
        await self.tools.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.plugin_manager is not None:
            await self.plugin_manager.terminate_all()
        if self.mcp_watcher is not None:
            await self.mcp_watcher.shutdown()
        await self.memory.shutdown()
        await self.event_bus.shutdown()
        self.session_manager.close()
