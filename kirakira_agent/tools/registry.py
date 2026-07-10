"""Kirakira Agent learning harness module."""

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any, Callable, Dict, List, Optional

from kirakira_agent.schema import JsonDict, ToolCall, ToolResult, ToolSpec


ToolHandler = Callable[..., str]


@dataclass
class Tool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._context: JsonDict = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            raise ValueError("Tool already registered: %s" % spec.name)
        self._tools[spec.name] = Tool(spec=spec, handler=handler)

    def specs(self) -> List[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def has(self, name: str) -> bool:
        return name in self._tools

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get_tool(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def set_context(self, **kwargs: Any) -> None:
        self._context = dict(kwargs)

    @property
    def context(self) -> JsonDict:
        return dict(self._context)

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, "Error: Unknown tool '%s'" % call.name, is_error=True)
        try:
            output = tool.handler(**call.arguments)
            if inspect.isawaitable(output):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    output = asyncio.run(output)
                else:
                    raise RuntimeError("Async tool '%s' requires execute_async" % call.name)
            return ToolResult(call.id, str(output), is_error=False)
        except Exception as exc:
            return ToolResult(call.id, "Error: %s" % exc, is_error=True)

    async def execute_async(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, "Error: Unknown tool '%s'" % call.name, is_error=True)
        try:
            output = tool.handler(**call.arguments)
            if inspect.isawaitable(output):
                output = await output
            return ToolResult(call.id, str(output), is_error=False)
        except Exception as exc:
            return ToolResult(call.id, "Error: %s" % exc, is_error=True)


def object_schema(properties: JsonDict, required: List[str]) -> JsonDict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }
