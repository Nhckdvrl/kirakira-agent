"""Tool hook execution, used by plugins to rewrite or block tool calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Protocol

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


class ToolExecutor:
    def __init__(self, hooks: List[ToolHook] | None = None) -> None:
        self._hooks = list(hooks or [])

    def add_hooks(self, hooks: List[ToolHook]) -> None:
        self._hooks.extend(hooks)

    async def execute(self, request: ToolExecutionRequest, invoker) -> ToolExecutionResult:
        args = dict(request.arguments)
        extra: List[str] = []
        for hook in self._hooks:
            if hook.event != "pre_tool_use":
                continue
            ctx = HookContext("pre_tool_use", request, dict(args))
            if not hook.matches(ctx):
                continue
            outcome = await hook.run(ctx)
            if outcome.updated_input is not None:
                args = dict(outcome.updated_input)
            if outcome.extra_message:
                extra.append(outcome.extra_message)
            if outcome.decision == "deny":
                return ToolExecutionResult("denied", outcome.reason or "工具调用被拦截", args, extra)
        try:
            output = await invoker(request.tool_name, args)
        except Exception as exc:
            error = str(exc)
            for hook in self._hooks:
                if hook.event == "post_tool_error":
                    ctx = HookContext("post_tool_error", request, dict(args), error=error)
                    if hook.matches(ctx):
                        outcome = await hook.run(ctx)
                        if outcome.extra_message:
                            extra.append(outcome.extra_message)
            return ToolExecutionResult("error", "工具执行出错: %s" % error, args, extra)
        for hook in self._hooks:
            if hook.event == "post_tool_use":
                ctx = HookContext("post_tool_use", request, dict(args), result=output)
                if hook.matches(ctx):
                    outcome = await hook.run(ctx)
                    if outcome.extra_message:
                        extra.append(outcome.extra_message)
        return ToolExecutionResult("success", str(output), args, extra)

