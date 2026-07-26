"""Kirakira Agent learning harness module."""

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass
import inspect
from typing import Any, Callable, Dict, List, Optional

from kirakira_agent.schema import JsonDict, ToolCall, ToolResult, ToolSpec


ToolHandler = Callable[..., Any]

# 单条工具结果进上下文前的硬上限(照 Reference agent/looping/constants.py)。
# 各 handler 自己的 truncate 只覆盖内置工具;MCP 远端与插件工具的返回值走不到那里,
# 必须在唯一出口(执行器)钳制,否则一个大响应就能撑爆当轮上下文。
_MAX_TOOL_RESULT_CHARS = 100_000


def _clamp_result_text(text: str) -> str:
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    original_len = len(text)
    return text[:_MAX_TOOL_RESULT_CHARS] + (
        "\n...[结果已截断，原始长度 %d 字符，超出上限 %d]"
        % (original_len, _MAX_TOOL_RESULT_CHARS)
    )


@dataclass
class Tool:
    spec: ToolSpec
    handler: ToolHandler
    deferred: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._shutdown_callbacks: List[Callable[[], Any]] = []
        self._context: ContextVar[JsonDict] = ContextVar(
            "kirakira_tool_context", default={}
        )

    def register(
        self, spec: ToolSpec, handler: ToolHandler, *, deferred: bool = False
    ) -> None:
        if spec.name in self._tools:
            raise ValueError("Tool already registered: %s" % spec.name)
        self._tools[spec.name] = Tool(
            spec=spec, handler=handler, deferred=bool(deferred)
        )

    def specs(self) -> List[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def visible_specs(self, unlocked: set[str] | None = None) -> List[ToolSpec]:
        allowed = unlocked or set()
        return [
            tool.spec
            for name, tool in self._tools.items()
            if not tool.deferred or name in allowed
        ]

    def is_deferred(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.deferred)

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def has(self, name: str) -> bool:
        return name in self._tools

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get_tool(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def set_context(self, **kwargs: Any) -> Token[JsonDict]:
        """Bind tool context to the current async task and return a reset token."""
        return self._context.set(dict(kwargs))

    def reset_context(self, token: Token[JsonDict]) -> None:
        self._context.reset(token)

    @property
    def context(self) -> JsonDict:
        return dict(self._context.get())

    async def shutdown(self) -> None:
        callbacks = list(reversed(self._shutdown_callbacks))
        self._shutdown_callbacks.clear()
        for callback in callbacks:
            result = callback()
            if inspect.isawaitable(result):
                await result

    def add_shutdown_callback(self, callback: Callable[[], Any]) -> None:
        if callback not in self._shutdown_callbacks:
            self._shutdown_callbacks.append(callback)

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, "Error: Unknown tool '%s'" % call.name, is_error=True)
        validation_error = _validate_arguments(tool.spec, call.arguments)
        if validation_error:
            return ToolResult(call.id, validation_error, is_error=True)
        try:
            output = tool.handler(**call.arguments)
            if inspect.isawaitable(output):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    output = asyncio.run(output)
                else:
                    raise RuntimeError("Async tool '%s' requires execute_async" % call.name)
            if isinstance(output, ToolResult):
                return ToolResult(
                    call.id,
                    _clamp_result_text(output.content),
                    output.is_error,
                    mobile_attention=output.mobile_attention,
                )
            text = _clamp_result_text(str(output))
            return ToolResult(call.id, text, is_error=_looks_like_error(text))
        except Exception as exc:
            return ToolResult(call.id, "Error: %s" % exc, is_error=True)

    async def execute_async(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, "Error: Unknown tool '%s'" % call.name, is_error=True)
        validation_error = _validate_arguments(tool.spec, call.arguments)
        if validation_error:
            return ToolResult(call.id, validation_error, is_error=True)
        try:
            if inspect.iscoroutinefunction(tool.handler):
                output = await tool.handler(**call.arguments)
            else:
                output = await asyncio.to_thread(tool.handler, **call.arguments)
                if inspect.isawaitable(output):
                    output = await output
            if isinstance(output, ToolResult):
                return ToolResult(
                    call.id,
                    _clamp_result_text(output.content),
                    output.is_error,
                    mobile_attention=output.mobile_attention,
                )
            text = _clamp_result_text(str(output))
            return ToolResult(call.id, text, is_error=_looks_like_error(text))
        except Exception as exc:
            return ToolResult(call.id, "Error: %s" % exc, is_error=True)


def object_schema(properties: JsonDict, required: List[str]) -> JsonDict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _looks_like_error(value: str) -> bool:
    return value.lstrip().lower().startswith(("error:", "tool execution error:"))


def _validate_arguments(spec: ToolSpec, arguments: JsonDict) -> str:
    if not isinstance(arguments, dict):
        return "Error: Tool '%s' arguments must be an object" % spec.name
    schema = spec.input_schema if isinstance(spec.input_schema, dict) else {}
    required = schema.get("required") or []
    for name in required:
        if name not in arguments:
            return "Error: Tool '%s' is missing required argument '%s'" % (
                spec.name,
                name,
            )
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return ""
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for name, value in arguments.items():
        field = properties.get(name)
        if not isinstance(field, dict):
            continue
        expected_name = field.get("type")
        expected = type_map.get(expected_name)
        if expected is not None and (
            not isinstance(value, expected)
            or expected_name in ("integer", "number")
            and isinstance(value, bool)
        ):
            return "Error: Tool '%s' argument '%s' must be %s" % (
                spec.name,
                name,
                expected_name,
            )
        choices = field.get("enum")
        if isinstance(choices, list) and value not in choices:
            return "Error: Tool '%s' argument '%s' must be one of %s" % (
                spec.name,
                name,
                choices,
            )
    return ""
