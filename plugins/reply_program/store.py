"""Durable reply inputs, effect facts and results in the Session transaction domain."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from bus.events import InboundMessage, OutboundMessage
from plugins.ledger.inputs import InputLedger
from plugins.ledger.owner_store import OwnerStore, StateConflict
from plugins.sources.service import SourceSessions

OWNER = "reply_programs"


def now():
    return datetime.now(UTC).isoformat()


def source_key(session_key, source):
    return "source:" + json.dumps([session_key, source], ensure_ascii=False)


def request_value(msg):
    return {
        "channel": msg.channel,
        "sender": msg.sender,
        "chat_id": msg.chat_id,
        "content": msg.content,
        "timestamp": msg.timestamp.isoformat(),
        "media": list(msg.media),
        "metadata": {k: v for k, v in msg.metadata.items() if not k.startswith("_")},
    }


def inbound(request):
    value = dict(request)
    value["timestamp"] = datetime.fromisoformat(value["timestamp"])
    return InboundMessage(**value)


def tool_chain(value):
    """Only closed facts enter replay; proposals and unknown effects never do."""
    groups = []
    for batch in value["batches"]:
        calls = []
        for call in batch["calls"]:
            receipt = value["effects"].get(call["key"])
            if receipt is None or receipt.get("result") is None:
                continue
            result = receipt["result"]
            calls.append(
                {
                    "call_id": call["id"],
                    "name": call["name"],
                    "effect_key": call["key"],
                    "arguments": result["arguments"],
                    "result": result["content"],
                    "status": result["status"],
                }
            )
        if calls:
            groups.append(
                {
                    "text": batch["text"],
                    "reasoning_content": batch["reasoning_content"],
                    "calls": calls,
                }
            )
    return groups


def unresolved(value):
    return {
        key: row
        for key, row in value["effects"].items()
        if row.get("started") and row.get("result") is None
    }


class ProgramStore:
    def __init__(self, sessions, sources=None, inputs=None):
        self.sessions = sessions
        self.store = OwnerStore(sessions)
        self.sources = sources or SourceSessions(sessions)
        self.inputs = inputs or InputLedger(sessions)

    def read(self, identity, session_key=None):
        row = self.store.read(OWNER, "program:" + identity)
        if row is None or (
            session_key is not None and row.value["session_key"] != session_key
        ):
            raise ValueError("program does not belong to this Session")
        return {"id": identity, "revision": row.version, **self._hydrate(row.value)}

    def list(self, session_key, limit=50):
        rows = [
            dict(id=key[8:], revision=row.version, **self._hydrate(row.value))
            for key, row in self.store.scan(OWNER, "program:")
            if row.value["session_key"] == session_key
        ]
        return sorted(rows, key=lambda v: v["created_at"], reverse=True)[:limit]

    def accept(self, msg, session_key, *, supersede=False, auto_resume=False):
        request = request_value(msg)
        # Transport time and request/reply correlation are not immutable content.
        stable = msg.metadata.get("input_message_id") or msg.metadata.get(
            "client_request_id"
        )
        identity = "pr_" + uuid4().hex
        fingerprint = {k: v for k, v in request.items() if k != "timestamp"}
        fingerprint["metadata"] = {
            k: v
            for k, v in fingerprint["metadata"].items()
            if k not in {"turnId", "resume_program_id"}
        }
        receipt = (
            "input:"
            + hashlib.sha256(
                json.dumps([session_key, msg.channel, stable], sort_keys=True).encode()
            ).hexdigest()
            if stable
            else None
        )
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        source = str(msg.metadata.get("source") or "conversation")
        with self.store.transaction() as tx:
            if receipt is not None and (prior := tx.read(OWNER, receipt)) is not None:
                if prior.value["fingerprint"] != fingerprint:
                    raise StateConflict(
                        "input identity reused with different content or routing"
                    )
                return prior.value["program_id"], False
            input_id = self.inputs.append(session_key, source, request, tx)
            epoch = None
            predecessor, interaction = None, identity
            if supersede:
                epoch, previous = self.sources.advance(
                    session_key, source, input_id, tx
                )
                prior_receipt = (
                    tx.read(OWNER, "input_program:" + previous)
                    if previous is not None
                    else None
                )
                previous = (
                    prior_receipt.value["program_id"]
                    if prior_receipt is not None
                    else None
                )
                prior_program = (
                    tx.read(OWNER, "program:" + previous)
                    if previous is not None
                    else None
                )
                if prior_program is not None and prior_program.value["status"] in {
                    "queued",
                    "running",
                    "superseded",
                }:
                    predecessor = previous
                    interaction = prior_program.value["interaction_id"]
            value = {
                "session_key": session_key,
                "source": source,
                "input_id": input_id,
                "interaction_id": interaction,
                "predecessor_id": predecessor,
                "status": "queued",
                "created_at": now(),
                "updated_at": now(),
                "epoch": epoch,
                "lease": None,
                "batches": [],
                "effects": {},
                "iteration": 0,
                "usage": [],
                "partial_ids": [],
                "history_head": None,
                "grant": None,
                "result": None,
                "error": None,
                "published_effects": [],
                "auto_resume": auto_resume,
                "recovery_pending": False,
            }
            tx.save(OWNER, "program:" + identity, value, expected_version=None)
            tx.save(
                OWNER,
                "input_program:" + input_id,
                {"program_id": identity, "session_key": session_key},
                expected_version=None,
            )
            if receipt:
                tx.save(
                    OWNER,
                    receipt,
                    {
                        "program_id": identity,
                        "session_key": session_key,
                        "fingerprint": fingerprint,
                    },
                    expected_version=None,
                )
        return identity, True

    def check_source(self, value, tx):
        self.sources.check(
            value["session_key"],
            value["source"],
            value["epoch"],
            tx,
            input_id=value["input_id"],
        )

    def is_current(self, identity):
        with self.store.transaction() as tx:
            row = tx.read(OWNER, "program:" + identity)
            try:
                self.check_source(row.value, tx)
            except StateConflict:
                return False
        return True

    def claim(self, identity, *, resume=False):
        with self.store.transaction() as tx:
            row = tx.read(OWNER, "program:" + identity)
            value = dict(row.value)
            allowed = {"paused", "failed"} if resume else {"queued"}
            if value["status"] not in allowed:
                raise StateConflict("program is not available to start/resume")
            if unresolved(value):
                raise StateConflict(
                    "unknown tool outcomes require reconciliation before resuming"
                )
            head = self.sessions._index.execute(
                "SELECT seq,id FROM messages WHERE session_key=? ORDER BY seq DESC LIMIT 1",
                (value["session_key"],),
            ).fetchone()
            if resume and value["history_head"] != (list(head) if head else None):
                raise StateConflict(
                    "Session history changed; inspect the program and start a new request"
                )
            value.update(
                status="running",
                lease=uuid4().hex,
                error=None,
                updated_at=now(),
                recovery_pending=False,
            )
            tx.save(OWNER, "program:" + identity, value, expected_version=row.version)
        return self._hydrate(value)

    def change(self, identity, lease, update, *, require_current=True):
        with self.store.transaction() as tx:
            row = tx.read(OWNER, "program:" + identity)
            value = dict(row.value)
            if value["status"] != "running" or value["lease"] != lease:
                raise StateConflict("program writer has expired")
            if require_current:
                self.check_source(value, tx)
            immutable = {
                key: value[key]
                for key in (
                    "input_id",
                    "session_key",
                    "source",
                    "epoch",
                    "interaction_id",
                    "predecessor_id",
                    "created_at",
                )
            }
            update(value)
            if any(value.get(key) != original for key, original in immutable.items()):
                raise StateConflict(
                    "program input identity and source guard are immutable"
                )
            value["updated_at"] = now()
            tx.save(OWNER, "program:" + identity, value, expected_version=row.version)
            return self._hydrate(value, tx)

    def settle(
        self,
        identity,
        lease,
        status,
        *,
        result=None,
        error=None,
        partial_ids=(),
        transaction=None,
        recovery_pending=False,
    ):
        def save(tx):
            row = tx.read(OWNER, "program:" + identity)
            value = dict(row.value)
            if value["status"] != "running" or value["lease"] != lease:
                raise StateConflict("program writer has expired")
            if result is not None:
                self.check_source(value, tx)
            head = self.sessions._index.execute(
                "SELECT seq,id FROM messages WHERE session_key=? ORDER BY seq DESC LIMIT 1",
                (value["session_key"],),
            ).fetchone()
            value.update(
                status="needs_reconciliation" if unresolved(value) else status,
                error=error,
                result=result,
                partial_ids=list(partial_ids),
                history_head=list(head) if head else None,
                recovery_pending=recovery_pending,
                lease=None,
                updated_at=now(),
            )
            if partial_ids:
                value["published_effects"] = [
                    key
                    for key, row in value["effects"].items()
                    if row.get("result") is not None
                ]
            tx.save(OWNER, "program:" + identity, value, expected_version=row.version)

        if transaction is not None:
            save(transaction)
        else:
            with self.store.transaction() as tx:
                save(tx)

    def delete_session(self, session_key, tx):
        for key, row in tx.scan(OWNER):
            if row.value.get("session_key") == session_key:
                tx.delete(OWNER, key, expected_version=row.version)

    def resolve(
        self,
        identity,
        session_key,
        effect_key,
        status,
        content,
        evidence,
        expected_revision,
    ):
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(
                "external outcome resolution requires an explicit operator explanation"
            )
        if status not in {"success", "error", "not_executed"}:
            raise ValueError("invalid resolution status")
        receipt_key = "resolution:" + identity + ":" + effect_key
        statement = {"status": status, "content": content, "evidence": evidence}
        with self.store.transaction() as tx:
            prior = tx.read(OWNER, receipt_key)
            if prior is not None:
                if prior.value["statement"] != statement:
                    raise StateConflict("resolution receipt cannot be changed")
            else:
                row = tx.read(OWNER, "program:" + identity)
                if row is None or row.value["session_key"] != session_key:
                    raise ValueError("program does not belong to this Session")
                value = dict(row.value)
                if (
                    row.version != expected_revision
                    or value["status"] != "needs_reconciliation"
                ):
                    raise StateConflict("program changed or has no unresolved effects")
                effect = value["effects"].get(effect_key)
                if (
                    effect is None
                    or not effect.get("started")
                    or effect.get("result") is not None
                ):
                    raise ValueError("effect is not awaiting reconciliation")
                effect["result"] = {
                    "status": "success" if status == "success" else "error",
                    "arguments": effect["arguments"],
                    "content": "Operator-verified outcome (" + status + "): " + content,
                    "mobile_attention": None,
                }
                effect["resolution"] = statement
                value["status"] = (
                    "needs_reconciliation" if unresolved(value) else "paused"
                )
                value["recovery_pending"] = False
                tx.save(
                    OWNER, "program:" + identity, value, expected_version=row.version
                )
                tx.save(
                    OWNER,
                    receipt_key,
                    {"session_key": session_key, "statement": statement},
                    expected_version=None,
                )
        return self.read(identity, session_key)

    def _hydrate(self, value, tx=None):
        message = self.inputs.read(
            value["input_id"], session_key=value["session_key"], transaction=tx
        )
        return {**value, "request": message.request}

    def recoverable(self):
        return sorted(
            [
                (key[8:], self._hydrate(row.value))
                for key, row in self.store.scan(OWNER, "program:")
                if row.value["status"] in {"running", "queued"}
            ],
            key=lambda row: row[1]["created_at"],
        )

    def pending_recovery(self):
        return [
            (key[8:], self._hydrate(row.value))
            for key, row in self.store.scan(OWNER, "program:")
            if row.value.get("recovery_pending") and row.value["status"] == "paused"
        ]

    @staticmethod
    def outbound(value):
        return OutboundMessage(**value["result"])
