from __future__ import annotations

from collections.abc import Awaitable, Callable


class MessagePushTool:
    """Channel registration boundary retained outside copied Reference files."""

    def __init__(self) -> None:
        self.channels: dict[str, dict[str, Callable[..., Awaitable[None]]]] = {}

    def register_channel(self, name: str, **senders) -> None:
        self.channels[name] = dict(senders)
