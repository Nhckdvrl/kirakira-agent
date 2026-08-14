"""Add the append-only session context-compaction ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from yoyo import step

from agent.migrations.context import current_migration_context


__depends__ = {"20260804_01_kirakira_origin"}


def _backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=False)
    source_conn = sqlite3.connect(source)
    backup_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(backup_conn)
    finally:
        backup_conn.close()
        source_conn.close()


def add_session_context_compaction(_connection: object) -> None:
    """Back up SessionDB, then apply only additive schema changes."""

    workspace = current_migration_context().workspace
    database = workspace / "sessions.db"
    if not database.exists():
        return
    with database.open("rb") as stream:
        sqlite_header = stream.read(16)
    if sqlite_header != b"SQLite format 3\x00":
        # The origin migration intentionally accepts opaque legacy fixtures. A
        # later SessionManager startup remains responsible for surfacing an
        # unreadable authoritative database.
        return
    backup = (
        workspace
        / ".kirakira"
        / "backups"
        / "session-context-compaction"
        / uuid4().hex
        / "sessions.db"
    )
    _backup(database, backup)
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "context_compaction_generation" not in columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN context_compaction_generation "
                "INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS session_compactions ("
            "session_key TEXT NOT NULL, generation INTEGER NOT NULL, "
            "parent_generation INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "trigger TEXT NOT NULL, summary_format_version INTEGER NOT NULL, "
            "summary TEXT NOT NULL, source_ref TEXT NOT NULL, "
            "source_plan_digest TEXT NOT NULL, source_from_seq INTEGER NOT NULL, "
            "consolidated_through_seq INTEGER NOT NULL, "
            "source_message_ids_json TEXT NOT NULL, retained_tail_json TEXT NOT NULL, "
            "model_runtime_id TEXT NOT NULL, model TEXT NOT NULL, "
            "context_window INTEGER NOT NULL, threshold_tokens INTEGER NOT NULL, "
            "hard_input_tokens INTEGER NOT NULL, keep_recent_tokens INTEGER NOT NULL, "
            "tokens_before INTEGER NOT NULL, tokens_after INTEGER NOT NULL, "
            "summary_usage_json TEXT NOT NULL, "
            "PRIMARY KEY(session_key, generation), UNIQUE(session_key, source_ref))"
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


steps = [step(add_session_context_compaction)]
