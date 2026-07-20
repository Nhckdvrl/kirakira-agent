"""Lifecycle contracts for the passive reply pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from kirakira_agent.events import InboundMessage, OutboundMessage


JsonDict = Dict[str, Any]


@dataclass
class TurnState:
    msg: InboundMessage
    session_key: str
    dispatch_outbound: bool = True
    session: Any = None
    extra_metadata: JsonDict = field(default_factory=dict)


@dataclass
class BeforeTurnCtx:
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime
    retrieved_memory_block: str
    history_messages: Tuple[JsonDict, ...]
    skill_names: List[str] = field(default_factory=list)
    abort: bool = False
    abort_reply: str = ""
    extra_hints: List[str] = field(default_factory=list)
    extra_metadata: JsonDict = field(default_factory=dict)


@dataclass
class BeforeReasoningCtx:
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime
    skill_names: List[str]
    retrieved_memory_block: str
    extra_hints: List[str] = field(default_factory=list)
    abort: bool = False
    abort_reply: str = ""


@dataclass
class PromptRenderCtx:
    session_key: str
    channel: str
    chat_id: str
    content: str
    media: Optional[List[str]]
    timestamp: datetime
    history: List[JsonDict]
    skill_names: Optional[List[str]]
    retrieved_memory_block: str
    extra_hints: List[str] = field(default_factory=list)
    system_sections_top: List[str] = field(default_factory=list)
    system_sections_bottom: List[str] = field(default_factory=list)


@dataclass
class BeforeStepCtx:
    session_key: str
    channel: str
    chat_id: str
    iteration: int
    input_tokens_estimate: int
    visible_tool_names: Tuple[str, ...]
    extra_hints: List[str] = field(default_factory=list)
    early_stop: bool = False
    early_stop_reply: str = ""


@dataclass(frozen=True)
class AfterStepCtx:
    session_key: str
    channel: str
    chat_id: str
    iteration: int
    context_tokens_estimate: int
    tools_called: Tuple[str, ...]
    partial_reply: str
    tools_used_so_far: Tuple[str, ...]
    tool_chain_partial: Tuple[JsonDict, ...]
    partial_thinking: str
    has_more: bool


@dataclass
class AfterReasoningCtx:
    session_key: str
    channel: str
    chat_id: str
    tools_used: Tuple[str, ...]
    thinking: str
    tool_chain: Tuple[JsonDict, ...]
    reply: str
    media: List[str] = field(default_factory=list)
    outbound_metadata: JsonDict = field(default_factory=dict)


@dataclass
class AfterReasoningResult:
    ctx: AfterReasoningCtx
    outbound: OutboundMessage


@dataclass
class AfterTurnCtx:
    session_key: str
    channel: str
    chat_id: str
    reply: str
    tools_used: Tuple[str, ...]
    thinking: str
    will_dispatch: bool
    extra_metadata: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class TurnCommitted:
    session_key: str
    channel: str
    chat_id: str
    user_content: str
    assistant_reply: str
    tools_used: Tuple[str, ...]


@dataclass(frozen=True)
class TurnStarted:
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime


@dataclass(frozen=True)
class ToolCallStarted:
    session_key: str
    channel: str
    chat_id: str
    call_id: str
    tool_name: str
    arguments: JsonDict
    # The model iteration which produced this call.  Kept optional at the end
    # so integrations constructing lifecycle events positionally remain valid.
    iteration: int = 0


@dataclass(frozen=True)
class ToolCallCompleted:
    session_key: str
    channel: str
    chat_id: str
    call_id: str
    tool_name: str
    arguments: JsonDict
    result: str
    status: str
    iteration: int = 0


@dataclass(frozen=True)
class StreamDeltaReady:
    session_key: str
    channel: str
    chat_id: str
    iteration: int
    content_delta: str = ""
    reasoning_delta: str = ""


@dataclass(frozen=True)
class TurnFinished:
    """Authoritative terminal event for every started turn.

    Consumers which render streaming output should treat ``outbound`` as the
    final, authoritative representation of the assistant message rather than
    appending it to previously received :class:`StreamDeltaReady` fragments.
    ``outbound`` is absent for failed and interrupted turns.
    """

    session_key: str
    channel: str
    chat_id: str
    status: str
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    will_dispatch: bool
    outbound: Optional[OutboundMessage] = None
    error: str = ""
