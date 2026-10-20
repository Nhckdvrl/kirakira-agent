"""Distribution boundaries not already covered by imported native contracts."""
import hashlib
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from plugins.ledger.contract import (
    CallRef,
    ContentPart,
    Input,
    Output,
    ToolCall,
    ToolResult,
    encode_body,
)
from plugins.ledger.log import MessageLog
from plugins.step_plan.driver import validate_endpoint
from scripts.migrate_legacy_workspace import migrate


def legacy_workspace(path, *, broken_result=False):
    path.mkdir()
    (path / "config.toml").write_text('[old]\napi_key="private-fixture-only"\n')
    with sqlite3.connect(path / "sessions.db") as db:
        db.execute("CREATE TABLE ledger_messages(message_id TEXT PRIMARY KEY,session_id TEXT,seq INTEGER,recorded_at TEXT,author TEXT,source TEXT,body_json TEXT,metadata_json TEXT)")
        bodies = [Input((ContentPart("text", "ORIGINAL_HISTORY"),)),
                  Output((ToolCall("historical-read", {"path": "a.txt"}),), "continue"),
                  ToolResult(CallRef("missing" if broken_result else "m1", 0), "success", (ContentPart("text", "EVIDENCE"),)),
                  Output((ContentPart("text", "HISTORICAL_ANSWER"),), "complete")]
        for seq, body in enumerate(bodies):
            db.execute("INSERT INTO ledger_messages VALUES(?,?,?,?,?,?,?,?)", (f"m{seq}", "akashic:legacy", seq,
                       "2026-09-01T12:30:00+00:00", "user" if seq == 0 else "assistant", "conversation", encode_body(body), "{}"))


def test_migration_preserves_facts_time_references_and_original_database(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    legacy_workspace(old)
    digest = hashlib.sha256((old / "sessions.db").read_bytes()).hexdigest()
    report = migrate(old, new)
    assert report["messages"] == 4 and report["sessions"] == 1
    assert hashlib.sha256((old / "sessions.db").read_bytes()).hexdigest() == digest
    assert (new / "legacy/config.toml").stat().st_mode & 0o777 == 0o600
    log = MessageLog(new / "sessions.db")
    try:
        rows = log.reader("akashic:legacy").snapshot()
        assert [row.message_id for row in rows] == ["m0", "m1", "m2", "m3"]
        assert rows[0].recorded_at.isoformat() == "2026-09-01T12:30:00+00:00"
        assert all(row.source == "legacy:conversation" for row in rows)
        assert rows[2].body.call_ref == CallRef("m1", 0)
        assert log.catalog().attributes("akashic:legacy").learning == "eligible"
    finally:
        log.close()


def test_migration_rejects_invalid_reference_without_partial_publication(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    legacy_workspace(old, broken_result=True)
    with pytest.raises(ValueError, match="earlier call"):
        migrate(old, new)
    assert not new.exists()
    assert not list(tmp_path.glob(".kirakira-migration-*"))


@pytest.mark.asyncio
async def test_legacy_runtime_is_rejected_before_resource_or_database_changes(tmp_path):
    from kirakira_agent.runtime import runtime
    old = tmp_path / "old"
    legacy_workspace(old)
    before = hashlib.sha256((old / "sessions.db").read_bytes()).hexdigest()
    files = set(old.iterdir())
    with pytest.raises(ValueError, match="explicit import"):
        async with runtime(old):
            pytest.fail("legacy runtime was admitted")
    assert set(old.iterdir()) == files
    assert hashlib.sha256((old / "sessions.db").read_bytes()).hexdigest() == before


def test_step_plan_rejects_cash_redirect_and_credential_urls():
    assert validate_endpoint("https://api.stepfun.com/step_plan/v1/").endswith("/step_plan/v1")
    for endpoint in ("https://api.stepfun.com/v1", "http://api.stepfun.com/step_plan/v1",
                     "https://user:secret@api.stepfun.com/step_plan/v1", "https://api.stepfun.com/step_plan/v1?cash=true",
                     "https://api.stepfun.com.attacker.invalid/step_plan/v1"):
        with pytest.raises(ValueError):
            validate_endpoint(endpoint)


def test_zsh_snapshot_can_parse_alias_function_name_and_restore_option(tmp_path):
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed")
    from plugins.host_execution.contract import ResolvedShell, ShellKind
    from plugins.host_execution.shell_runtime import _assemble_snapshot
    snapshot = _assemble_snapshot(ResolvedShell(ShellKind.ZSH, Path(zsh)), {
        "options": b"aliases", "functions": b"alias conflict='echo wrong'\nconflict () { echo actual; }\n",
        "aliases": b"", "env": b"",
    }, {})
    result = subprocess.run([zsh, "-fc", snapshot + "[[ -o aliases ]] || exit 7\nunalias conflict\nconflict\n"],
                            check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "actual"
