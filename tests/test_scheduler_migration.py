"""Versioned migration validates the whole snapshot before replacing anything."""

import json
from pathlib import Path

import pytest

from agent.migrations.runner import MigrationRunner
from plugins.scheduler.store import JobStore


def migrate(root):
    return MigrationRunner(
        repo_root=Path(__file__).resolve().parents[1],
        config_path=root / "config.toml",
        workspace=root,
    ).run()


def old_job(**changes):
    return {
        "id": "job_fixture",
        "channel": "web",
        "chat_id": "origin",
        "tier": "soft",
        "message": "",
        "prompt": "local task",
        "run_at": "2027-01-01T00:00:00+00:00",
        **changes,
    }


def save(root, jobs):
    path = root / ".kirakira" / "schedules.json"
    path.parent.mkdir()
    original = (
        json.dumps({"jobs": jobs}, ensure_ascii=False, indent=3) + "\n"
    ).encode()
    path.write_bytes(original)
    return path, original


def test_legacy_migration_keeps_origin_recurrence_status_and_exact_backup(tmp_path):
    path, original = save(
        tmp_path,
        [
            old_job(),
            old_job(
                id="job_notify",
                tier="instant",
                message="verbatim",
                trigger="every",
                interval_seconds=60,
                remaining_runs=-1,
                status="cancelled",
                timezone="Asia/Tokyo",
            ),
        ],
    )
    migrate(tmp_path)
    state = JobStore(path).read()
    assert state.jobs["job_fixture"].action == "run"
    assert state.jobs["job_fixture"].session_key == "web:origin"
    notification = state.jobs["job_notify"]
    assert notification.action == "notify" and notification.status == "cancelled"
    assert notification.interval_seconds == 60 and notification.remaining_runs == -1
    backups = list(path.parent.glob("schedules.json.pre-v2-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    after = path.read_bytes()
    assert migrate(tmp_path).state == "current" and path.read_bytes() == after


def test_old_setup_wizard_empty_sentinel_migrates(tmp_path):
    path = tmp_path / ".kirakira" / "schedules.json"
    path.parent.mkdir()
    path.write_bytes(b"[]\n")
    migrate(tmp_path)
    assert not JobStore(path).read().jobs
    assert next(path.parent.glob("schedules.json.pre-v2-*")).read_bytes() == b"[]\n"


@pytest.mark.parametrize(
    "bad",
    [
        old_job(extra_field="unknown"),
        old_job(run_at="2027-01-01T00:00:00"),
        old_job(created_at="invalid"),
        old_job(channel=""),
        old_job(remaining_runs=0),
        old_job(interval_seconds=True),
        old_job(interval_seconds="60"),
        old_job(trigger="every"),
        old_job(cron_expr="no such cron"),
        old_job(timezone="invalid/zone"),
        old_job(prompt=""),
    ],
)
def test_invalid_legacy_snapshot_is_not_changed_or_backed_up(tmp_path, bad):
    path, original = save(tmp_path, [old_job(id="valid"), bad])
    with pytest.raises(RuntimeError, match="migration failed"):
        migrate(tmp_path)
    assert path.read_bytes() == original
    assert not list(path.parent.glob("schedules.json.pre-v2-*"))
