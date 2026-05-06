from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: JsonDict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: JsonDict = field(default_factory=dict)


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw: Optional[JsonDict] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def assistant_message_from_response(response: ModelResponse) -> JsonDict:
    message: JsonDict = {"role": "assistant", "content": response.text or ""}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.arguments,
                },
            }
            for call in response.tool_calls
        ]
    return message


def tool_result_message(result: ToolResult) -> JsonDict:
    return {
        "role": "tool",
        "tool_call_id": result.tool_call_id,
        "content": result.content,
        "is_error": result.is_error,
    }
