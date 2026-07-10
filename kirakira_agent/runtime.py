"""Akashic-style passive runtime: AgentLoop, PassiveTurnPipeline, and Reasoner."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kirakira_agent.bus import MessageBus
from kirakira_agent.context_builder import ContextBuilder
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
    TurnState,
)
from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.models.base import ModelClient
from kirakira_agent.schema import ModelResponse, ToolCall, assistant_message_from_response, tool_result_message
from kirakira_agent.session import SessionManager
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
    ) -> ReasonerResult:
        render_ctx = PromptRenderCtx(
            session_key=session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
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
        )

    async def run(
        self,
        messages: List[JsonDict],
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        request_text: str,
    ) -> ReasonerResult:
        tools_used: List[str] = []
        tool_chain: List[JsonDict] = []
        final_reply = ""
        final_thinking = ""
        for iteration in range(self.config.max_iterations):
            visible_names = tuple(self.tools.names())
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

            response = await asyncio.to_thread(
                self.model_client.complete,
                messages,
                self.tools.specs(),
                "",
                self.config.model,
                self.config.max_tokens,
            )
            final_reply = response.text or ""
            final_thinking = response.reasoning_content or final_thinking
            messages.append(assistant_message_from_response(response))
            if not response.tool_calls:
                await self._after_step(
                    session_key, channel, chat_id, iteration, messages, (), final_reply,
                    tools_used, tool_chain, final_thinking, has_more=False,
                )
                return ReasonerResult(final_reply, tools_used, tool_chain, final_thinking)

            group = {
                "text": response.text or "",
                "reasoning_content": response.reasoning_content or "",
                "calls": [],
            }
            for call in response.tool_calls:
                result = await self._execute_tool(call, session_key, channel, chat_id, request_text)
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
                        type(
                            "_Result",
                            (),
                            {
                                "tool_call_id": call.id,
                                "content": result["content"],
                                "is_error": result["status"] != "success",
                            },
                        )()
                    )
                )
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
        return ReasonerResult(
            reply=final_reply or "工具调用轮次已用尽，我先基于已有结果停在这里。",
            tools_used=tools_used,
            tool_chain=tool_chain,
            thinking=final_thinking,
        )

    async def _execute_tool(
        self,
        call: ToolCall,
        session_key: str,
        channel: str,
        chat_id: str,
        request_text: str,
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

        async def invoke(tool_name: str, arguments: JsonDict) -> str:
            result = await self.tools.execute_async(ToolCall(call.id, tool_name, arguments))
            return result.content

        result = await self._tool_executor.execute(request, invoke)
        content = result.output
        if result.extra_messages:
            content += "\n\n" + "\n".join(result.extra_messages)
        return {"content": content, "status": result.status, "arguments": result.final_arguments}

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
    ) -> None:
        self.bus = bus
        self.event_bus = event_bus
        self.session_manager = session_manager
        self.memory = memory
        self.tools = tools
        self.reasoner = reasoner
        self.config = config
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
        session = self.session_manager.get_or_create(key)
        state = TurnState(msg=msg, session_key=key, dispatch_outbound=dispatch_outbound, session=session)
        history = session.get_history(max_messages=self.config.history_window)
        retrieved = "" if msg.metadata.get("skip_memory_retrieval") else self.memory.build_retrieval_block(msg.content)
        skill_mentions = self._collect_skill_mentions(msg.content)
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
        before_turn = await _run_plugin_modules(self._before_turn_modules, before_turn)
        state.extra_metadata.update(before_turn.extra_metadata)
        if before_turn.abort:
            return await self._dispatch_if_needed(state, before_turn.abort_reply)

        self.tools.set_context(
            session_key=key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            current_timestamp=msg.timestamp.isoformat(),
        )
        before_reasoning = BeforeReasoningCtx(
            session_key=key,
            channel=msg.channel,
            chat_id=msg.chat_id,
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
            history=history,
            retrieved_memory_block=before_reasoning.retrieved_memory_block,
            skill_names=before_reasoning.skill_names,
            extra_hints=before_reasoning.extra_hints,
        )
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
        outbound = OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=after_ctx.reply,
            thinking=after_ctx.thinking,
            media=list(after_ctx.media),
            metadata=dict(after_ctx.outbound_metadata),
        )
        result = AfterReasoningResult(ctx=after_ctx, outbound=outbound)
        return await self._commit_and_dispatch(state, result)

    async def _commit_and_dispatch(self, state: TurnState, result: AfterReasoningResult) -> OutboundMessage:
        session = state.session
        msg = state.msg
        session.add_message("user", msg.content, media=msg.media)
        session.add_message(
            "assistant",
            result.outbound.content,
            media=result.outbound.media,
            thinking=result.outbound.thinking,
            tools_used=list(result.ctx.tools_used),
            tool_chain=list(result.ctx.tool_chain),
            **state.extra_metadata,
        )
        self.memory.consolidate_turn(session, msg.content, result.outbound.content)
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
        return result.outbound

    async def _dispatch_if_needed(self, state: TurnState, content: str) -> OutboundMessage:
        outbound = OutboundMessage(channel=state.msg.channel, chat_id=state.msg.chat_id, content=content)
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


class AgentLoop:
    def __init__(self, *, bus: MessageBus, pipeline: PassiveTurnPipeline) -> None:
        self.bus = bus
        self.pipeline = pipeline
        self._running = False
        self._active_tasks: Dict[str, asyncio.Task[Any]] = {}

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                item = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            key = item.session_key
            task = asyncio.create_task(self.pipeline.run(item, key))
            self._active_tasks[key] = task
            try:
                await task
            except Exception as exc:
                logger.exception("failed to process inbound")
                await self.bus.publish_outbound(
                    OutboundMessage(item.channel, item.chat_id, "出错：%s" % exc)
                )
            finally:
                await self.bus.complete_inbound(item)
                self._active_tasks.pop(key, None)

    def stop(self) -> None:
        self._running = False


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

    def add_tool_hooks(self, hooks: List[ToolHook]) -> None:
        self.reasoner.add_tool_hooks(hooks)

    async def start_background(self) -> list[asyncio.Task[Any]]:
        tasks = [
            asyncio.create_task(self.loop.run(), name="agent_loop"),
            asyncio.create_task(self.bus.dispatch_outbound(), name="bus_dispatch"),
        ]
        if self.channel_host is not None:
            await self.channel_host.start_all()
        return tasks

    async def stop_background(self, tasks: list[asyncio.Task[Any]]) -> None:
        self.loop.stop()
        self.bus.stop()
        if self.channel_host is not None:
            await self.channel_host.stop_all()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
