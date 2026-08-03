"""Tool hook execution, used by plugins to rewrite or block tool calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Literal, Optional, Protocol

from core.schema import ToolResult

logger = logging.getLogger(__name__)

JsonDict = Dict[str, Any]
HookEvent = Literal["pre_tool_use", "post_tool_use", "post_tool_error"]


@dataclass(frozen=True)
class ToolExecutionRequest:
    session_key: str
    channel: str
    chat_id: str
    tool_name: str
    arguments: JsonDict
    call_id: str = ""
    request_text: str = ""


@dataclass
class HookContext:
    event: HookEvent
    request: ToolExecutionRequest
    current_arguments: JsonDict
    result: Any = None
    error: str = ""


@dataclass
class HookOutcome:
    decision: Literal["allow", "deny"] = "allow"
    updated_input: JsonDict | None = None
    reason: str = ""
    extra_message: str = ""


class ToolHook(Protocol):
    name: str
    event: HookEvent

    def matches(self, ctx: HookContext) -> bool: ...
    async def run(self, ctx: HookContext) -> HookOutcome: ...


@dataclass
class ToolExecutionResult:
    status: Literal["success", "denied", "error"]
    output: str
    final_arguments: JsonDict
    extra_messages: List[str] = field(default_factory=list)
    # 工具自己声明的 turn 级注意力标记;聚合与合法性校验在 runtime 侧
    # (照 Reference agent/core/passive_turn.py:1656)。
    mobile_attention: Optional[str] = None


class ToolExecutor:
    def __init__(
        self,
        hooks: List[ToolHook] | None = None,
        *,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._hooks = list(hooks or [])
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def add_hooks(self, hooks: List[ToolHook]) -> None:
        self._hooks.extend(hooks)

    async def execute(self, request: ToolExecutionRequest, invoker) -> ToolExecutionResult:
        args = dict(request.arguments)
        extra: List[str] = []
        for hook in self._hooks:
            if hook.event != "pre_tool_use":
                continue
            ctx = HookContext("pre_tool_use", request, dict(args))
            try:
                if not hook.matches(ctx):
                    continue
                outcome = await hook.run(ctx)
            except Exception as exc:
                logger.exception("pre-tool hook failed: %s", hook.name)
                return ToolExecutionResult(
                    "error",
                    "Error: Pre-tool hook '%s' failed: %s" % (hook.name, exc),
                    args,
                    extra,
                )
            if outcome.updated_input is not None:
                args = dict(outcome.updated_input)
            if outcome.extra_message:
                extra.append(outcome.extra_message)
            if outcome.decision == "deny":
                return ToolExecutionResult("denied", outcome.reason or "工具调用被拦截", args, extra)
        try:
            # ``wait_for`` 会把协程放进新 Task，让本 turn 按 owner task 绑定的
            # RuntimeSnapshot 失效。``asyncio.timeout`` 在当前 Task 内取消，
            # 同时保留 Reference 的“整轮固定同一代工具”语义。
            async with asyncio.timeout(self.timeout_seconds):
                invoked = await invoker(request.tool_name, args)
        except TimeoutError:
            error = "tool timed out after %.1f seconds" % self.timeout_seconds
            await self._run_error_hooks(request, args, error, extra)
            return ToolExecutionResult("error", "Error: %s" % error, args, extra)
        except Exception as exc:
            error = str(exc)
            await self._run_error_hooks(request, args, error, extra)
            return ToolExecutionResult("error", "工具执行出错: %s" % error, args, extra)
        attention: Optional[str] = None
        if isinstance(invoked, ToolResult):
            output = invoked.content
            attention = invoked.mobile_attention
            if invoked.is_error:
                await self._run_error_hooks(request, args, output, extra)
                return ToolExecutionResult(
                    "error", output, args, extra, mobile_attention=attention
                )
        else:
            output = str(invoked)
        for hook in self._hooks:
            if hook.event == "post_tool_use":
                ctx = HookContext("post_tool_use", request, dict(args), result=output)
                try:
                    if hook.matches(ctx):
                        outcome = await hook.run(ctx)
                        if outcome.extra_message:
                            extra.append(outcome.extra_message)
                except Exception:
                    logger.exception("post-tool hook failed: %s", hook.name)
        return ToolExecutionResult(
            "success", str(output), args, extra, mobile_attention=attention
        )

    async def _run_error_hooks(
        self,
        request: ToolExecutionRequest,
        args: JsonDict,
        error: str,
        extra: List[str],
    ) -> None:
        for hook in self._hooks:
            if hook.event != "post_tool_error":
                continue
            ctx = HookContext("post_tool_error", request, dict(args), error=error)
            try:
                if not hook.matches(ctx):
                    continue
                outcome = await hook.run(ctx)
                if outcome.extra_message:
                    extra.append(outcome.extra_message)
            except Exception:
                logger.exception("post-tool-error hook failed: %s", hook.name)
