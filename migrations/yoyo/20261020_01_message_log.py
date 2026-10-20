"""Create the typed journal; owners migrate inline facts transactionally on load."""

import sqlite3
from yoyo import step
from agent.migrations.context import current_migration_context
from plugins.ledger.log import ensure_message_log

__depends__ = {"20261019_02_workspace_owners"}


def create_message_log(_connection):
    path = current_migration_context().workspace / "sessions.db"
    if not path.exists():
        return
    with path.open("rb") as source:
        if source.read(16) != b"SQLite format 3\x00":
            return
    with sqlite3.connect(path) as db:
        ensure_message_log(db)


steps = [step(create_message_log)]
