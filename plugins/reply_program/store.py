"""Durable reply inputs, effect facts and results in the Session transaction domain."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from bus.events import InboundMessage, OutboundMessage
from core.common.frozen_json import json_value
from plugins.ledger.inputs import InputLedger
from plugins.ledger.message import (
    CallRef,
    ContentPart,
    Control,
    Output,
    ToolCall,
    ToolResult,
)
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
    def __init__(self, sessions, sources=None, inputs=None, messages=None):
        self.sessions = sessions
        self.store = OwnerStore(sessions)
        self.sources = sources or SourceSessions(sessions)
        self.inputs = inputs or InputLedger(sessions)
        self.messages = messages if messages is not None else sessions.message_log
        # Move old inline model/tool facts once, atomically. Existing program and
        # input identities remain valid; a failed migration leaves the old row.
        with self.store.transaction() as tx:
            for key, row in sorted(
                tx.scan(OWNER, "program:"), key=lambda item: item[1].value["created_at"]
            ):
                if row.value.get("journal_version") != 1:
                    self._save_program(
                        tx, key[8:], self._hydrate(row.value, tx), row.version
                    )
                    if row.value["status"] in {
                        "paused",
                        "failed",
                        "needs_reconciliation",
                    }:
                        self._control(
                            row.value,
                            "failure" if row.value["status"] == "failed" else "pause",
                            tx,
                            row.value.get("error"),
                        )

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
            self._save_program(tx, identity, value, None)
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

    def steer(self, binding, msg):
        with self.store.transaction() as tx:
            row = tx.read(OWNER, "program:" + binding.identity)
            value = self._hydrate(row.value, tx)
            if value["status"] != "running" or value["lease"] != binding.lease:
                raise StateConflict("steer target is no longer running")
            self.check_source(value, tx)
            # The client identity is scoped to this Session, not a global ID.
            stable = hashlib.sha256(
                json.dumps(
                    [binding.session_key, msg.metadata["client_request_id"]]
                ).encode()
            ).hexdigest()
            identity = "steer:" + stable
            request = request_value(msg)
            prior = self.messages.get(identity, transaction=tx)
            if prior is not None:
                if identity not in value.get("steer_ids", []):
                    raise StateConflict(
                        "steer identity already belongs to another reply program"
                    )
                previous = self.inputs.read(
                    identity, session_key=binding.session_key, transaction=tx
                )
                if (
                    previous.request["content"] != msg.content
                    or previous.request["media"] != msg.media
                ):
                    raise StateConflict("steer identity reused with different facts")
            else:
                self.inputs.append(
                    binding.session_key, value["source"], request, tx, identity=identity
                )
            ids = value.setdefault("steer_ids", [])
            if identity not in ids:
                ids.append(identity)
            self._save_program(tx, binding.identity, value, row.version)
            return self._hydrate(value, tx)

    def project_steers(self, session, binding):
        ids = []
        existing = {message["id"] for message in session.messages}
        for identity in binding.value.get("steer_ids", []):
            request = self.inputs.read(identity, session_key=session.key).request
            if identity not in existing:
                session.add_message(
                    "user",
                    request["content"],
                    id=identity,
                    media=request["media"],
                    source=binding.value["source"],
                    program_id=binding.identity,
                    input_id=identity,
                    inbound_timestamp=request["timestamp"],
                    skip_post_memory=True,
                )
            ids.append(identity)
        return ids

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
            value = self._hydrate(row.value, tx)
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
            if resume:
                self._control(value, "resume", tx)
            self._save_program(tx, identity, value, row.version)
        return self._hydrate(value)

    def change(self, identity, lease, update, *, require_current=True):
        with self.store.transaction() as tx:
            row = tx.read(OWNER, "program:" + identity)
            value = self._hydrate(row.value, tx)
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
            self._save_program(tx, identity, value, row.version)
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
            value = self._hydrate(row.value, tx)
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
            if status in {"paused", "failed"}:
                self._control(
                    value, "failure" if status == "failed" else "pause", tx, error
                )
            self._save_program(tx, identity, value, row.version)

        if transaction is not None:
            save(transaction)
        else:
            with self.store.transaction() as tx:
                save(tx)

    def delete_session(self, session_key, tx):
        for key, row in tx.scan(OWNER):
            if row.value.get("session_key") == session_key:
                tx.delete(OWNER, key, expected_version=row.version)

    def redact(self, message_ids, tx):
        """Invalidate recovery records when their explicit history is erased."""
        programs = set()
        for identity in message_ids:
            row = tx.db.execute(
                "SELECT extra_json FROM messages WHERE id=?", (identity,)
            ).fetchone()
            if row is not None and (program := json.loads(row[0]).get("program_id")):
                programs.add(program)
        for identity in programs:
            row = tx.read(OWNER, "program:" + identity)
            if row is None:
                continue
            if row.value["status"] in {"running", "queued"}:
                raise StateConflict(
                    "stop the active program before erasing its history"
                )
            inputs = [row.value["input_id"], *row.value.get("steer_ids", [])]
            for mid in inputs:
                tx.db.execute("DELETE FROM ledger_messages WHERE message_id=?", (mid,))
                tx.db.execute(
                    "DELETE FROM ledger_messages WHERE json_extract(metadata_json,'$.input_id')=?",
                    (mid,),
                )
            tx.db.execute(
                "DELETE FROM ledger_messages WHERE json_extract(metadata_json,'$.program_id')=?",
                (identity,),
            )
            for key, record in tx.scan(OWNER):
                if (
                    key == "program:" + identity
                    or record.value.get("program_id") == identity
                    or key.startswith("resolution:" + identity + ":")
                ):
                    tx.delete(OWNER, key, expected_version=record.version)

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
                value = self._hydrate(row.value, tx)
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
                self._save_program(tx, identity, value, row.version)
                tx.save(
                    OWNER,
                    receipt_key,
                    {"session_key": session_key, "statement": statement},
                    expected_version=None,
                )
        return self.read(identity, session_key)

    def _hydrate(self, value, tx=None):
        value = json.loads(json.dumps(value))
        value.setdefault("result", None)
        for index, batch in enumerate(value["batches"]):
            if "message_id" not in batch:
                continue
            message = self.messages.get(
                batch["message_id"], session_id=value["session_key"], transaction=tx
            )
            if message is None or not isinstance(message.body, Output):
                raise StateConflict("program refers to a missing model output")
            parts = message.body.parts
            values = {
                part.kind: json_value(part.value)
                for part in parts
                if isinstance(part, ContentPart)
            }
            calls = [part for part in parts if isinstance(part, ToolCall)]
            value["batches"][index] = {
                "text": values.get("text", ""),
                "reasoning_content": values.get("reasoning", ""),
                "iteration": batch["iteration"],
                "calls": [
                    {
                        **json_value(ref),
                        "name": call.binding_id,
                        "arguments": json_value(call.arguments),
                    }
                    for ref, call in zip(message.metadata["calls"], calls, strict=True)
                ],
            }
        for effect in value["effects"].values():
            identity = effect.get("result_message_id")
            if identity is None:
                effect.setdefault("result", None)
                continue
            message = self.messages.get(
                identity, session_id=value["session_key"], transaction=tx
            )
            if message is None or not isinstance(message.body, ToolResult):
                raise StateConflict("program refers to a missing tool result")
            parts = {part.kind: json_value(part.value) for part in message.body.parts}
            effect["result"] = {**parts["execution"], "content": parts["text"]}
        if value.get("result_message_id"):
            message = self.messages.get(
                value["result_message_id"],
                session_id=value["session_key"],
                transaction=tx,
            )
            if message is None or not isinstance(message.body, Output):
                raise StateConflict("program refers to a missing final output")
            parts = {part.kind: json_value(part.value) for part in message.body.parts}
            value["result"] = {
                **parts["outbound"],
                "content": parts["text"],
                "thinking": parts["reasoning"],
                "media": parts["media"],
            }
        message = self.inputs.read(
            value["input_id"], session_key=value["session_key"], transaction=tx
        )
        return {**value, "request": message.request}

    def _save_program(self, tx, identity, value, version):
        raw = json.loads(json.dumps(value))
        raw["journal_version"] = 1
        raw.pop("request", None)
        call_refs = {}
        for index, batch in enumerate(value["batches"]):
            mid = f"batch:{identity}:{index}"
            calls = batch["calls"]
            self.messages.append(
                value["session_key"],
                value["source"],
                "assistant",
                Output(
                    (
                        ContentPart("text", batch["text"]),
                        ContentPart("reasoning", batch["reasoning_content"]),
                        *(ToolCall(call["name"], call["arguments"]) for call in calls),
                    ),
                    "continue",
                ),
                message_id=mid,
                metadata={
                    "program_id": identity,
                    "calls": [{"id": call["id"], "key": call["key"]} for call in calls],
                },
                transaction=tx,
            )
            raw["batches"][index] = {"message_id": mid, "iteration": batch["iteration"]}
            call_refs.update(
                {
                    call["key"]: CallRef(mid, offset + 2)
                    for offset, call in enumerate(calls)
                }
            )
        for key, effect in value["effects"].items():
            result = effect.get("result")
            raw["effects"][key].pop("result", None)
            if result is None:
                continue
            message = self.messages.append(
                value["session_key"],
                value["source"],
                "tool",
                ToolResult(
                    call_refs[key],
                    result["status"]
                    if result["status"] in {"success", "denied", "error", "interrupted"}
                    else "error",
                    (
                        ContentPart("text", result["content"]),
                        ContentPart(
                            "execution",
                            {k: v for k, v in result.items() if k != "content"},
                        ),
                    ),
                ),
                message_id="result:" + key,
                metadata={"program_id": identity},
                transaction=tx,
            )
            raw["effects"][key]["result_message_id"] = message.message_id
        result = value.get("result")
        if result is not None:
            message = self.messages.append(
                value["session_key"],
                value["source"],
                "assistant",
                Output(
                    (
                        ContentPart("text", result["content"]),
                        ContentPart("reasoning", result.get("thinking", "")),
                        ContentPart("media", result.get("media", [])),
                        ContentPart(
                            "outbound",
                            {
                                k: v
                                for k, v in result.items()
                                if k not in {"content", "thinking", "media"}
                            },
                        ),
                    ),
                    "complete" if result["content"] else "quiet",
                ),
                message_id=value.get("result_message_id")
                or result.get("session_message_id")
                or "output:" + identity,
                metadata={"program_id": identity},
                transaction=tx,
            )
            raw["result_message_id"] = message.message_id
        raw.pop("result", None)
        tx.save(OWNER, "program:" + identity, raw, expected_version=version)

    def _control(self, value, action, tx, reason=None):
        head = self.messages.head(
            value["session_key"], source=value["source"], transaction=tx
        )
        self.messages.append(
            value["session_key"],
            value["source"],
            "runtime",
            Control(action, head, reason),
            transaction=tx,
            metadata={"input_id": value["input_id"]},
        )

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
