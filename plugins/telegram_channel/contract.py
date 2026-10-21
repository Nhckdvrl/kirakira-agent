"""Optional coordination between transient Telegram previews and durable delivery."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from agent.plugin_composition import ServiceKey


# Identity is a SHA256 of the bot credential, never the credential itself.
class DraftDelivery(Protocol):
    def register(self, identity: str, reader: object) -> Callable[[], None]: ...
    def delivering(self, identity: str, chat_id: int, message_id: str | None) -> AbstractAsyncContextManager[None]: ...


DRAFT_DELIVERY = ServiceKey[DraftDelivery]("telegram.draft-delivery.v1")
