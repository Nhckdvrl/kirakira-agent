"""Append-only Message sequence for inputs, model outputs, tool results and control.

Owner records keep leases and references. Reply recovery reads bodies here;
the older chat table is the compatibility history for memory/search clients.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from .message import (
    CallRef,
    ContentPart,
    Control,
    Input,
    Message,
    Output,
    ToolCall,
    ToolResult,
    decode_body,
    encode_body,
    freeze_metadata,
)
from .owner_store import OwnerStore, OwnerTransaction, StateConflict


def ensure_message_log(db):
    db.execute("""CREATE TABLE IF NOT EXISTS ledger_messages (
        message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
        recorded_at TEXT NOT NULL, author TEXT NOT NULL, source TEXT NOT NULL,
        body_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
        UNIQUE(session_id, seq))""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS ledger_messages_source ON ledger_messages(session_id,source,seq)"
    )


def _decode(row):
    return (
        None
        if row is None
        else Message(
            row[0],
            row[1],
            row[2],
            datetime.fromisoformat(row[3]),
            row[4],
            row[5],
            decode_body(row[6]),
            json.loads(row[7]),
        )
    )


class MessageLog:
    def __init__(self, sessions):
        self.store = OwnerStore(sessions)

    def import_compat_history(self):
        """Import pre-program history once per ID; retain original IDs and text."""
        with self.store.transaction() as tx:
            rows = tx.db.execute(
                "SELECT m.id,m.session_key,m.seq,m.role,m.content,m.ts,m.extra_json FROM messages m "
                "WHERE json_extract(m.extra_json,'$.program_id') IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM ledger_messages l WHERE l.message_id=m.id) ORDER BY m.session_key,m.seq"
            ).fetchall()
            for row in rows:
                self.import_compat_row(row, tx)

    def import_compat_row(self, row, tx):
        identity, session, seq, role, content, timestamp, extra_json = row
        extra = json.loads(extra_json)
        if extra.get("program_id"):
            return
        source = str(extra.get("source") or "conversation")
        try:
            recorded = datetime.fromisoformat(timestamp)
            if recorded.utcoffset() is None:
                recorded = recorded.replace(tzinfo=UTC)
        except ValueError:
            recorded = datetime.now(UTC)
        history = {
            "seq": seq,
            "timestamp": timestamp,
            **{k: v for k, v in extra.items() if k != "tool_chain"},
        }
        metadata = {"imported_history": True}
        if role != "assistant":
            self.append(
                session,
                source,
                role or "user",
                Input((ContentPart("text", content), ContentPart("history", history))),
                message_id=identity,
                metadata=metadata,
                recorded_at=recorded,
                transaction=tx,
            )
            return
        for index, group in enumerate(extra.get("tool_chain") or []):
            calls = group.get("calls") or []
            parts = [
                ContentPart("text", group.get("text") or ""),
                ContentPart("reasoning", group.get("reasoning_content") or ""),
            ]
            for call in calls:
                arguments = call.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {"legacy_arguments": arguments}
                if not isinstance(arguments, dict):
                    arguments = {"legacy_arguments": arguments}
                parts.append(ToolCall(call.get("name") or "legacy/unknown", arguments))
            output = self.append(
                session,
                source,
                "assistant",
                Output(tuple(parts), "continue"),
                message_id=f"{identity}:batch:{index}",
                metadata=metadata,
                recorded_at=recorded,
                transaction=tx,
            )
            for offset, call in enumerate(calls):
                self.append(
                    session,
                    source,
                    "tool",
                    ToolResult(
                        CallRef(output.message_id, offset + 2),
                        "success" if call.get("status") == "success" else "error",
                        (ContentPart("text", call.get("result") or ""),),
                    ),
                    message_id=f"{identity}:result:{index}:{offset}",
                    metadata=metadata,
                    recorded_at=recorded,
                    transaction=tx,
                )
        self.append(
            session,
            source,
            "assistant",
            Output(
                (ContentPart("text", content), ContentPart("history", history)),
                "continue"
                if extra.get("interrupted")
                else "complete"
                if content
                else "quiet",
            ),
            message_id=identity,
            metadata=metadata,
            recorded_at=recorded,
            transaction=tx,
        )

    def append(
        self,
        session_id,
        source,
        author,
        body,
        *,
        message_id=None,
        metadata=None,
        transaction=None,
        recorded_at=None,
    ):
        if transaction is None:
            with self.store.transaction() as tx:
                return self.append(
                    session_id,
                    source,
                    author,
                    body,
                    message_id=message_id,
                    metadata=metadata,
                    transaction=tx,
                    recorded_at=recorded_at,
                )
        identity = "msg_" + uuid4().hex if message_id is None else message_id
        existing = self.get(identity, transaction=transaction)
        payload = encode_body(body, allow_legacy=False)
        if existing is not None:
            if (
                existing.session_id != session_id
                or existing.source != source
                or existing.author != author
                or encode_body(existing.body) != payload
                or existing.metadata != freeze_metadata(metadata or {})
            ):
                raise StateConflict("message identity reused with different facts")
            return existing
        db = transaction.db
        seq = self.head(session_id, transaction=transaction) + 1
        message = Message(
            identity,
            session_id,
            seq,
            recorded_at or datetime.now(UTC),
            author,
            source,
            body,
            metadata or {},
        )
        if isinstance(body, ToolResult):
            call = self.get(body.call_ref.message_id, transaction=transaction)
            if (
                call is None
                or call.session_id != session_id
                or call.source != source
                or not isinstance(call.body, Output)
                or body.call_ref.part_index >= len(call.body.parts)
                or not isinstance(call.body.parts[body.call_ref.part_index], ToolCall)
            ):
                raise StateConflict(
                    "tool result must reference an accepted call in this source"
                )
            for prior in self.read(
                session_id, source=source, after_seq=call.seq, transaction=transaction
            ):
                if (
                    isinstance(prior.body, ToolResult)
                    and prior.body.call_ref == body.call_ref
                ):
                    raise StateConflict("tool call already has a durable result")
        if isinstance(body, Control) and body.through_seq > self.head(
            session_id, source=source, transaction=transaction
        ):
            raise StateConflict("control exceeds the accepted source prefix")
        db.execute(
            "INSERT INTO ledger_messages VALUES (?,?,?,?,?,?,?,?)",
            (
                identity,
                session_id,
                seq,
                message.recorded_at.isoformat(),
                author,
                source,
                payload,
                json.dumps(
                    message.metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                ),
            ),
        )
        return message

    def get(self, message_id, *, session_id=None, transaction=None):
        if transaction is None:
            with self.store.sessions._index_lock:
                return self.get(
                    message_id,
                    session_id=session_id,
                    transaction=OwnerTransaction(self.store.sessions._index),
                )
        result = _decode(
            transaction.db.execute(
                "SELECT * FROM ledger_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        )
        if (
            result is not None
            and session_id is not None
            and result.session_id != session_id
        ):
            raise ValueError("message does not belong to this Session")
        return result

    def read(
        self,
        session_id,
        *,
        source=None,
        after_seq=-1,
        through_seq=None,
        transaction=None,
    ):
        if transaction is None:
            with self.store.sessions._index_lock:
                return self.read(
                    session_id,
                    source=source,
                    after_seq=after_seq,
                    through_seq=through_seq,
                    transaction=OwnerTransaction(self.store.sessions._index),
                )
        sql, args = (
            "SELECT * FROM ledger_messages WHERE session_id=? AND seq>?",
            [session_id, after_seq],
        )
        if source is not None:
            sql += " AND source=?"
            args.append(source)
        if through_seq is not None:
            sql += " AND seq<=?"
            args.append(through_seq)
        return tuple(
            _decode(row) for row in transaction.db.execute(sql + " ORDER BY seq", args)
        )

    def head(self, session_id, *, source=None, transaction=None):
        if transaction is None:
            with self.store.sessions._index_lock:
                return self.head(
                    session_id,
                    source=source,
                    transaction=OwnerTransaction(self.store.sessions._index),
                )
        sql, args = (
            "SELECT COALESCE(MAX(seq),-1) FROM ledger_messages WHERE session_id=?",
            [session_id],
        )
        if source is not None:
            sql += " AND source=?"
            args.append(source)
        return int(transaction.db.execute(sql, args).fetchone()[0])
