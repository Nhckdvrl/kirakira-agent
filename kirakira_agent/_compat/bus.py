"""Reference event contracts backed by Kirakira's event bus."""

from __future__ import annotations

from typing import Protocol

from kirakira_agent.event_bus import EventBus, EventSubscription
from kirakira_agent.lifecycle import TurnCommitted


class EventPublisher(Protocol):
    def enqueue(self, event: object): ...


__all__ = ["EventBus", "EventPublisher", "EventSubscription", "TurnCommitted"]
