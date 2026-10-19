"""Enforce tool grants, durable intent and result boundaries."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import uuid4

from agent.core.execution import current_execution
from agent.tool_hooks import ToolExecutionRequest, ToolExecutor
from bus.events_lifecycle import ToolCallCompleted, ToolCallStarted
from core.schema import JsonDict, ToolCall, ToolResult


class ToolInvoker:
    def __init__(self, tools, event_bus):
        self.tools, self.event_bus = tools, event_bus
        self._tool_executor = ToolExecutor()

    def add_hooks(self, hooks):
        self._tool_executor.add_hooks(hooks)

    async def execute(
        self,
        call: ToolCall,
        session_key: str,
        channel: str,
        chat_id: str,
        request_text: str,
        iteration: int,
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
                iteration=iteration,
            )
        )

        registry = tools if tools is not None else self.tools

        execution_error = None
        unsettled = False

        async def invoke(tool_name: str, arguments: JsonDict) -> ToolResult:
            nonlocal execution_error, unsettled
            try:
                ctx = current_execution()
                if ctx:
                    ctx.check()
                    if not ctx.policy.allows(tool_name):
                        return ToolResult(
                            call.id,
                            "Error: rewritten tool is outside this execution's grant",
                            is_error=True,
                        )
                operation_id = uuid4().hex
                if ctx:
                    ctx.tool_started = True
                    ctx.record(
                        "tool_intent",
                        {
                            "operation_id": operation_id,
                            "call_id": call.id,
                            "name": tool_name,
                            "arguments": arguments,
                            "iteration": iteration,
                        },
                    )
                    unsettled = ctx.store is not None
                result = await registry.execute_async(
                    ToolCall(call.id, tool_name, arguments)
                )
                if ctx:
                    ctx.record(
                        "tool_result", {"operation_id": operation_id, **asdict(result)}
                    )
                    unsettled = False
                return result
            except Exception as exc:
                execution_error = exc
                raise

        result = await self._tool_executor.execute(request, invoke)
        if execution_error is not None:
            raise execution_error
        # Concurrent siblings legitimately have pending intents. Only this call's
        # failed/timeout outcome can block its completion; finish() checks the run.
        if unsettled:
            raise RuntimeError(
                "Tool outcome is uncertain; execution requires reconciliation"
            )
        content = result.output
        if result.extra_messages:
            content += "\n\n" + "\n".join(result.extra_messages)
        # 照 Reference passive_turn.py:1656——只有成功的工具可以声明注意力标记,
        # 失败的工具声明属于工具实现 bug,必须 fail loud 而不是静默丢弃。
        if result.mobile_attention is not None:
            if result.status != "success":
                raise RuntimeError("失败工具不能声明 mobile_attention")
            if result.mobile_attention != "confirmation":
                raise RuntimeError(
                    "无效 mobile_attention: %s" % result.mobile_attention
                )
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
                iteration=iteration,
            )
        )
        return {
            "content": content,
            "status": result.status,
            "arguments": result.final_arguments,
            "mobile_attention": result.mobile_attention,
        }

    async def deny(
        self,
        call: ToolCall,
        session_key: str,
        channel: str,
        chat_id: str,
        iteration: int,
        reason: str,
    ) -> JsonDict:
        started = ToolCallStarted(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            call_id=call.id,
            tool_name=call.name,
            arguments=dict(call.arguments),
            iteration=iteration,
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
                iteration=iteration,
            )
        )
        return {
            "content": reason,
            "status": "denied",
            "arguments": dict(call.arguments),
        }
