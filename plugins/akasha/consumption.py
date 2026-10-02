"""Read durable publication positions; never infer new work from notifications.

First activation records a cutover head. Restart resumes the persisted cursor,
without replaying pre-activation history or treating timestamps as queue IDs.
"""

from contextlib import closing
from pathlib import Path
import sqlite3

from plugins.akasha.store import AkashaStore


def publications(path: Path, after: int, limit: int = 64) -> list[dict]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='message_publications'").fetchone():
            return []
        return [dict(row) for row in db.execute(
            "SELECT p.position,p.message_id,p.session_key,m.extra_json FROM message_publications p "
            "LEFT JOIN messages m ON m.id=p.message_id WHERE p.position>? ORDER BY p.position LIMIT ?",
            (after, limit),
        )]


def initialize_cursor(store: AkashaStore, source: Path) -> None:
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='message_publications'").fetchone()
        head = db.execute("SELECT coalesce(max(position),0) FROM message_publications").fetchone()[0] if exists else 0
        stream = db.execute("SELECT stream_id FROM message_publication_stream WHERE singleton=1").fetchone()[0] if exists else ""
    with store.transaction():
        binding = store.db.execute("SELECT stream_id FROM akasha_source_binding").fetchone()
        cursor = store.db.execute("SELECT position FROM akasha_source_cursor").fetchone()
        if binding and binding[0] and binding[0] != stream:
            raise ValueError("memory source stream changed; explicit rebuild required")
        if cursor and cursor[0] > head:
            raise ValueError("memory source stream moved backwards; restore matching source and graph")
        store.db.execute("INSERT INTO akasha_source_binding VALUES (1, ?) "
                         "ON CONFLICT(singleton) DO UPDATE SET stream_id=excluded.stream_id", (stream,))
        store.db.execute("INSERT OR IGNORE INTO akasha_source_cursor VALUES (1, ?)", (head,))


def skip_publication(store: AkashaStore, publication: dict, reason: str) -> None:
    with store.transaction():
        store.db.execute("INSERT OR IGNORE INTO akasha_source_skips VALUES (?, ?, ?)",
                         (publication["position"], publication["message_id"], reason))
        store.db.execute("UPDATE akasha_source_cursor SET position=? WHERE singleton=1",
                         (publication["position"],))
