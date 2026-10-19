"""Add owner state without filling or changing any existing Session directory."""

import sqlite3

from yoyo import step

from agent.migrations.context import current_migration_context

__depends__ = {"20261019_01_scheduler_v2"}


def create_owner_state(_connection):
    path = current_migration_context().workspace / "sessions.db"
    if not path.exists():
        return
    with path.open("rb") as source:
        if source.read(16) != b"SQLite format 3\x00":
            return
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS owner_records (
            owner TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
            value_json TEXT NOT NULL, PRIMARY KEY(owner,key))""")


steps = [step(create_owner_state)]
