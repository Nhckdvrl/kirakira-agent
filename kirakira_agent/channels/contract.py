"""Channel contracts."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from kirakira_agent.bus import MessageBus
from kirakira_agent.event_bus import EventBus
from kirakira_agent.session import SessionManager


class Channel(Protocol):
    name: str

    async def start(self, ctx: "ChannelContext") -> None: ...
    async def stop(self) -> None: ...


@dataclass
class ChannelContext:
    bus: MessageBus
    session_manager: SessionManager
    event_bus: EventBus
    workspace: Path
    log: logging.Logger
    interrupt: Optional[Callable[[str], bool]] = None
    memory: Any = None
    push_tool: Any = None
    attachment_store: Any = None
    http_resources: Any = None
    interrupt_controller: Any = None
    mobile_bot_commands: list[tuple[str, str]] = field(default_factory=list)
