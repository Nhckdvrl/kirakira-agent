"""Channel contracts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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

