from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.migrations.runner import MigrationRunner
from bootstrap.workspace_lock import WorkspaceInstanceLock


ROOT = Path(__file__).parents[1]
ORIGIN_ID = "20260804_01_kirakira_origin"
CONTEXT_COMPACTION_ID = "20260815_01_session_context_compaction"
PROJECT_MIGRATIONS = (ORIGIN_ID, CONTEXT_COMPACTION_ID)


def _runner(root: Path, *, repo_root: Path = ROOT) -> MigrationRunner:
    return MigrationRunner(
        repo_root=repo_root,
        config_path=root / "config.toml",
        workspace=root / "workspace",
    )


def _applied_ids(ledger: Path) -> list[str]:
    with sqlite3.connect(ledger) as connection:
        rows = connection.execute(
            "SELECT migration_id FROM _yoyo_migration ORDER BY migration_id"
        ).fetchall()
    return [str(row[0]) for row in rows]


def test_origin_records_current_schema_without_touching_business_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    workspace = root / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    fixtures = {
        workspace / "sessions.db": b"session-v1",
        memory / "coremem.db": b"default-memory-v1",
        memory / "akasha.db": b"akasha-v1",
        memory / "MEMORY.md": "stable profile\n".encode(),
    }
    for path, payload in fixtures.items():
        path.write_bytes(payload)

    outcome = _runner(root).run()

    assert outcome.state == "migrated"
    assert outcome.migrations == PROJECT_MIGRATIONS
    assert _applied_ids(workspace / "migrations.sqlite3") == sorted(PROJECT_MIGRATIONS)
    for path, payload in fixtures.items():
        assert path.read_bytes() == payload


def test_origin_is_idempotent(tmp_path: Path) -> None:
    runner = _runner(tmp_path / "state")

    assert runner.run().migrations == PROJECT_MIGRATIONS
    assert runner.run().state == "current"
    assert runner.run().migrations == ()


def test_context_compaction_migration_backs_up_and_adds_schema(tmp_path: Path) -> None:
    root = tmp_path / "state"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    database = workspace / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE sessions (key TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, metadata_json TEXT NOT NULL, "
            "last_consolidated INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES ('s', 'created', 'updated', '{}', 7)"
        )

    assert _runner(root).run().migrations == PROJECT_MIGRATIONS

    with sqlite3.connect(database) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        cursor = connection.execute(
            "SELECT context_compaction_generation, last_consolidated FROM sessions"
        ).fetchone()
        ledger_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_compactions'"
        ).fetchone()
    assert "context_compaction_generation" in columns
    assert cursor == (0, 7)
    assert ledger_exists == (1,)
    backups = list(
        (workspace / ".kirakira/backups/session-context-compaction").glob(
            "*/sessions.db"
        )
    )
    assert len(backups) == 1


def test_workspace_lock_prevents_concurrent_migration(tmp_path: Path) -> None:
    runner = _runner(tmp_path / "state")
    lock = WorkspaceInstanceLock(runner.workspace)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already owned"):
            runner.run()
    finally:
        lock.release()

    assert not runner.ledger_path.exists()


def test_workspace_lock_releases_for_next_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = WorkspaceInstanceLock(workspace)
    second = WorkspaceInstanceLock(workspace)

    first.acquire()
    with pytest.raises(RuntimeError, match="already owned"):
        second.acquire()
    first.release()

    second.acquire()
    second.release()


def test_new_dependent_migration_runs_once(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    catalog = repo / "migrations/yoyo"
    catalog.mkdir(parents=True)
    root = tmp_path / "state"
    workspace = root / "workspace"
    marker = workspace / "order.log"
    (catalog / "base.py").write_text(
        "from yoyo import step\n"
        "__depends__ = set()\n"
        f"def apply(_connection):\n    open({str(marker)!r}, 'a').write('base\\n')\n"
        "steps = [step(apply)]\n",
        encoding="utf-8",
    )
    (catalog / "next.py").write_text(
        "from yoyo import step\n"
        "__depends__ = {'base'}\n"
        f"def apply(_connection):\n    open({str(marker)!r}, 'a').write('next\\n')\n"
        "steps = [step(apply)]\n",
        encoding="utf-8",
    )

    runner = _runner(root, repo_root=repo)
    assert runner.run().migrations == ("base", "next")
    assert runner.run().migrations == ()
    assert marker.read_text(encoding="utf-8") == "base\nnext\n"


def test_ledger_supports_workspace_uri_characters(tmp_path: Path) -> None:
    root = tmp_path / "state with # and ?"

    assert _runner(root).run().migrations == PROJECT_MIGRATIONS
    assert (root / "workspace/migrations.sqlite3").is_file()
