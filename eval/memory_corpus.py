"""Frozen public histories, imported offline without answers or execution grants."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from plugins.ledger.contract import ContentPart, Input, Message, Output, encode_body
from plugins.ledger.log import MessageLog, SessionAttributes, encode_attributes


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def category(case):
    return "abstention" if "_abs" in case["question_id"] else case["question_type"]


def select_cases(path: Path, *, per_category: int, seed: str, offset: int = 0):
    """Select by ID hash before looking at answers, outcomes, or history length."""
    cases = json.loads(path.read_text())
    result = []
    for kind in sorted({category(case) for case in cases}):
        pool = [case for case in cases if category(case) == kind]
        pool.sort(key=lambda case: hashlib.sha256(
            (seed + ":" + case["question_id"]).encode()).hexdigest())
        result.extend(pool[offset:offset + per_category])
    return result


def timestamp(value: str) -> datetime:
    # The dataset provides session dates, not individual message times.
    return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M").replace(tzinfo=UTC)


def import_history(case, directory: Path, *, existing=False):
    """A dedicated, new benchmark DB only; never import labels into the Ledger.

    Session timestamps are retained; microsecond sequence offsets merely encode
    within-session ordering and do not claim original message time precision.
    """
    directory.mkdir(parents=True, exist_ok=existing)
    path = directory / "sessions.db"
    if not existing:
        MessageLog(path).close()
    messages = []
    sessions = {}
    gold_messages = set()
    gold_sessions = set(case["answer_session_ids"])
    occurrences = Counter()
    for original_sid, date, rows in zip(case["haystack_session_ids"], case["haystack_dates"],
                               case["haystack_sessions"], strict=True):
        occurrence = occurrences[original_sid]
        occurrences[original_sid] += 1
        sid = original_sid if occurrence == 0 else original_sid + "#occurrence-" + str(occurrence)
        stamp = timestamp(date)
        sessions[sid] = stamp
        for seq, row in enumerate(rows):
            if row["role"] not in {"user", "assistant"}:
                raise ValueError("unsupported benchmark history role")
            identity = hashlib.sha256(json.dumps([case["question_id"], sid, seq]).encode()).hexdigest()
            parts = (ContentPart("text", row["content"]),)
            body = Input(parts) if row["role"] == "user" else Output(parts, "complete")
            metadata = {} if sid == original_sid else {"evaluation_session_id": original_sid}
            message = Message(identity, sid, seq, stamp + timedelta(microseconds=seq),
                              row["role"], "evaluation-history", body, metadata)
            messages.append(message)
            if row.get("has_answer"):
                gold_messages.add(identity)
    if existing:
        log = MessageLog(path)
        try:
            for message in messages:
                actual = log.reader(message.session_id).get(message.message_id)
                if actual is None or actual != message:
                    raise ValueError("cached canonical history differs from the fixed dataset")
        finally:
            log.close()
        return messages, gold_messages, gold_sessions
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        for sid, stamp in sessions.items():
            count = sum(message.session_id == sid for message in messages)
            db.execute("INSERT INTO sessions(key,created_at,updated_at,metadata,attributes,next_seq) VALUES(?,?,?,?,?,?)",
                       (sid, stamp.isoformat(), stamp.isoformat(), "{}",
                        encode_attributes(SessionAttributes()), count))
        db.executemany("INSERT INTO messages(id,session_key,seq,ts,author,source,body,metadata) VALUES(?,?,?,?,?,?,?,?)",
                       [(m.message_id, m.session_id, m.seq, m.recorded_at.isoformat(), m.author,
                         m.source, encode_body(m.body), json.dumps(dict(m.metadata))) for m in messages])
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("benchmark import has invalid references")
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("benchmark import failed integrity check")
    return messages, gold_messages, gold_sessions
