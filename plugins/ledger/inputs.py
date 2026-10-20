"""Accepted requests are Input messages, never a second owner-record payload."""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from core.common.frozen_json import json_value

from .message import ContentPart, Input
from .owner_store import OwnerStore

OWNER = "ledger_inputs"  # Read only during the one-time legacy migration.


@dataclass(frozen=True)
class InputMessage:
    id: str
    session_key: str
    source: str
    request: dict
    accepted_at: str


class InputLedger:
    def __init__(self, sessions):
        self.store = OwnerStore(sessions)
        self.messages = sessions.message_log

    def append(
        self, session_key, source, request, tx, *, identity=None, recorded_at=None
    ):
        origin = {
            key: value
            for key, value in request.items()
            if key not in {"content", "media"}
        }
        message = self.messages.append(
            session_key,
            source,
            str(request.get("sender") or "user"),
            Input(
                (
                    ContentPart("origin", origin),
                    ContentPart("text", request["content"]),
                    ContentPart("media", request.get("media", [])),
                )
            ),
            message_id=identity or "input_" + uuid4().hex,
            transaction=tx,
            recorded_at=recorded_at,
        )
        return message.message_id

    def read(self, identity, *, session_key=None, transaction=None):
        message = self.messages.get(
            identity, session_id=session_key, transaction=transaction
        )
        if message is None:
            if transaction is None:
                with self.store.transaction() as tx:
                    return self.read(identity, session_key=session_key, transaction=tx)
            row = transaction.read(OWNER, identity)
            if row is not None:
                value = row.value
                if session_key is not None and value["session_key"] != session_key:
                    raise ValueError("input does not belong to this Session")
                self.append(
                    value["session_key"],
                    value["source"],
                    value["request"],
                    transaction,
                    identity=identity,
                    recorded_at=datetime.fromisoformat(value["accepted_at"]),
                )
                transaction.delete(OWNER, identity, expected_version=row.version)
                message = self.messages.get(
                    identity, session_id=session_key, transaction=transaction
                )
        if message is None or not isinstance(message.body, Input):
            raise ValueError("input does not belong to this Session")
        parts = {part.kind: json_value(part.value) for part in message.body.parts}
        request = {
            **parts["origin"],
            "content": parts["text"],
            "media": parts.get("media", []),
        }
        return InputMessage(
            identity,
            message.session_id,
            message.source,
            request,
            message.recorded_at.isoformat(),
        )
