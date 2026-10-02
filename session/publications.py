"""Durable notification positions, committed atomically with assistant messages.

Messages remain the only content truth. Positions never get reused, including
after message deletion; consumers can audit a missing source explicitly.
"""

import sqlite3


def ensure_publications(db: sqlite3.Connection) -> None:
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
