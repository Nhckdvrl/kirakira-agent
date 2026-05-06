from dataclasses import dataclass
from typing import Callable, Dict, List

from kirakira_agent.schema import JsonDict, ToolCall, ToolResult, ToolSpec


ToolHandler = Callable[..., str]


@dataclass
class Tool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

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

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, "Error: Unknown tool '%s'" % call.name, is_error=True)
        try:
            output = tool.handler(**call.arguments)
            return ToolResult(call.id, str(output), is_error=False)
        except Exception as exc:
            return ToolResult(call.id, "Error: %s" % exc, is_error=True)


def object_schema(properties: JsonDict, required: List[str]) -> JsonDict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }
