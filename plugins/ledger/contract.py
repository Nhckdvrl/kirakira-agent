from agent.composition import ServiceKey
from .message import (  # noqa: F401
    CallRef, ContentPart, Control, Input, Message, Output, ToolCall, ToolResult,
    body_to_dict, decode_body, encode_body,
)

SESSIONS = ServiceKey("ledger.sessions.v1")

INPUTS = ServiceKey("ledger.inputs.v1")

MESSAGES = ServiceKey("ledger.messages.v1")
