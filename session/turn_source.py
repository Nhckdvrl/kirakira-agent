"""Canonical eligible turns resolved from original committed message IDs."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from bus.events_lifecycle import TurnCommitted


def is_memory_source(message: dict) -> bool:
    """Only human conversation sources may teach memory or drive presence."""
    source = str(message.get("source") or "legacy-unattributed")
    return (
        source in {"conversation", "programmatic", "legacy-unattributed"}
        and not message.get("skip_post_memory")
        and not message.get("proactive")
    )


@dataclass(frozen=True)
class SourceMessage:
    id: str
    session_key: str
    seq: int
    role: str
    content: str
    ts: str
    salience: float | None = None


def load_committed_turn_messages(
    session_db_path: Path,
    event: TurnCommitted,
) -> list[SourceMessage]:
    """Stable IDs select the exact committed pair; legacy content lookup must be unique."""
    if not session_db_path.exists():
        return []
    with closing(
        sqlite3.connect(f"{session_db_path.resolve().as_uri()}?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        params = [event.session_key]
        condition = "u.session_key = ?"
        if event.assistant_message_id:
            condition += " AND a.id = ?"
            params.append(event.assistant_message_id)
            if event.user_message_id:
                condition += " AND u.id = ?"
                params.append(event.user_message_id)
        else:
            condition += " AND u.content = ? AND a.content = ?"
            params.extend(
                [
                    event.persisted_user_message or event.input_message,
                    event.assistant_response,
                ]
            )
        pairs = db.execute(
            "SELECT u.id AS uid, a.id AS aid FROM messages u JOIN messages a "
            "ON a.session_key=u.session_key AND a.seq=u.seq+1 AND a.role='assistant' "
            "WHERE u.role='user' AND " + condition,
            params,
        ).fetchall()
        if not pairs:
            return []
        if len(pairs) != 1:
            raise ValueError("ambiguous legacy commit; stable message IDs required")
        rows = db.execute(
            "SELECT * FROM messages WHERE id IN (?, ?) ORDER BY seq",
            (pairs[0]["uid"], pairs[0]["aid"]),
        ).fetchall()
    if any(not is_memory_source(json.loads(row["extra_json"] or "{}")) for row in rows):
        return []
    if rows[1]["content"] == "[interrupted]" or any(
        not row["content"].strip() for row in rows
    ):
        return []
    return [
        SourceMessage(
            id=row["id"],
            session_key=row["session_key"],
            seq=row["seq"],
            role=row["role"],
            content=row["content"],
            ts=row["ts"],
        )
        for row in rows
    ]
