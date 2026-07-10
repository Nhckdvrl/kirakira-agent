"""Typed message contracts for channels, the bus, and the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List


JsonDict = Dict[str, Any]


@dataclass
class InboundMessage:
    channel: str
    sender: str
    chat_id: str
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now().astimezone())
    media: List[str] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        override = str(self.metadata.get("session_key_override") or "").strip()
        if override:
            return override
        return "%s:%s" % (self.channel, self.chat_id)

    @property
    def context_channel(self) -> str:
        return str(self.metadata.get("context_channel") or self.channel).strip()

    @property
    def context_chat_id(self) -> str:
        return str(self.metadata.get("context_chat_id") or self.chat_id).strip()


@dataclass
class OutboundMessage:
    channel: str
    chat_id: str
    content: str
    thinking: str = ""
    reply_to: str = ""
    media: List[str] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

