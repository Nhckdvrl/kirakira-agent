"""Immutable accepted inputs in the same ledger transaction as owner state."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .owner_store import OwnerStore

OWNER = "ledger_inputs"


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
        sessions.register_initializer(
            "ledger_inputs", lambda session, tx: None, on_delete=self.delete_session
        )

    @staticmethod
    def append(session_key, source, request, tx):
        identity = "input_" + uuid4().hex
        tx.save(
            OWNER,
            identity,
            {
                "session_key": session_key,
                "source": source,
                "request": request,
                "accepted_at": datetime.now(UTC).isoformat(),
            },
            expected_version=None,
        )
        return identity

    def read(self, identity, *, session_key=None, transaction=None):
        row = (
            transaction.read(OWNER, identity)
            if transaction is not None
            else self.store.read(OWNER, identity)
        )
        if row is None or (
            session_key is not None and row.value["session_key"] != session_key
        ):
            raise ValueError("input does not belong to this Session")
        return InputMessage(identity, **row.value)

    @staticmethod
    def delete_session(session_key, tx):
        for key, row in tx.scan(OWNER):
            if row.value["session_key"] == session_key:
                tx.delete(OWNER, key, expected_version=row.version)
