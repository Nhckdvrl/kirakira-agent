"""Add durable assistant publication positions without backfilling old history."""

import sqlite3
from contextlib import closing

from yoyo import step

from agent.migrations.context import current_migration_context

__depends__ = {"20260815_01_session_context_compaction"}


def add_publications(_connection: object) -> None:
    path = current_migration_context().workspace / "sessions.db"
    if not path.exists():
        return
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            return
    with closing(sqlite3.connect(path)) as db, db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
            return
        # Kept inline so this append-only migration cannot change with runtime code.
        db.execute("""CREATE TABLE IF NOT EXISTS message_publication_stream (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), stream_id TEXT NOT NULL
        )""")
        db.execute("INSERT OR IGNORE INTO message_publication_stream VALUES (1, lower(hex(randomblob(16))))")
        db.execute("""CREATE TABLE IF NOT EXISTS message_publications (
            position INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            session_key TEXT NOT NULL
        )""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS publish_assistant_message
            AFTER INSERT ON messages WHEN new.role = 'assistant'
            BEGIN
                INSERT INTO message_publications(message_id, session_key)
                VALUES (new.id, new.session_key);
            END""")


steps = [step(add_publications)]
