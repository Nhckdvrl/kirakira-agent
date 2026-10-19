"""Source admission, durable heads and invalidation belong to the source owner."""

import json
from contextlib import contextmanager

from plugins.ledger.owner_store import OwnerStore, StateConflict

OWNER = "sources"


def source_key(session_key, source):
    return json.dumps([session_key, source], ensure_ascii=False)


class SourceSessions:
    def __init__(self, sessions):
        self.store = OwnerStore(sessions)
        self.active = {}
        sessions.register_initializer(
            "sources", lambda session, tx: None, on_delete=self.delete_session
        )

    def advance(self, session_key, source, input_id, tx):
        key = source_key(session_key, source)
        old = tx.read(OWNER, key)
        current = tx.save(
            OWNER,
            key,
            {"head_id": input_id, "session_key": session_key},
            expected_version=None if old is None else old.version,
        )
        return current.version, None if old is None else old.value["head_id"]

    @staticmethod
    def check(session_key, source, epoch, tx, *, input_id=None):
        if epoch is None:
            return
        head = tx.read(OWNER, source_key(session_key, source))
        if (
            head is None
            or head.version != epoch
            or (input_id is not None and head.value["head_id"] != input_id)
        ):
            raise StateConflict("program superseded by a newer accepted input")

    def accepted(self, session_key, source):
        cancel = self.active.get((session_key, source))
        if cancel is not None:
            cancel()

    @contextmanager
    def task(self, session_key, source, cancel):
        key = (session_key, source)
        self.active[key] = cancel
        try:
            yield
        finally:
            if self.active.get(key) is cancel:
                self.active.pop(key)

    @staticmethod
    def channel_policy(msg):
        if msg.metadata.get("subagent_completion"):
            # A job report is an independent source. It cannot invalidate a
            # user's conversation or become a synthetic user instruction.
            msg.metadata["source"] = "job:" + str(
                msg.metadata.get("subagent_task_id") or msg.sender
            )
            return False
        return True

    @staticmethod
    def delete_session(session_key, tx):
        for key, row in tx.scan(OWNER):
            if row.value["session_key"] == session_key:
                tx.delete(OWNER, key, expected_version=row.version)
