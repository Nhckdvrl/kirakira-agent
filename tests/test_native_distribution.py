"""Distribution boundaries not already covered by imported native contracts."""
import hashlib
import json
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
from scripts.migrate_legacy_workspace import activate, migrate


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


def test_complete_workspace_migration_merges_json_wal_and_profiles_and_activates(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    (old / "config.toml").write_text('[runtime]\nworkspace="."\n[agent]\nsystem_prompt="Kirakira persona"\nmax_iterations=20\nmax_tokens=8192\n[channels.telegram]\nenabled=false\n')
    (old / "sessions").mkdir()
    document = {"key": "cli:old", "created_at": "2026-07-20T12:00:00+00:00",
                "updated_at": "2026-07-20T12:00:01+00:00", "metadata": {"turn_count": 1},
                "messages": [{"role": "user", "content": "remember cedar", "timestamp": "2026-07-20T12:00:00+00:00"},
                             {"role": "assistant", "content": "cedar remembered", "timestamp": "2026-07-20T12:00:01+00:00", "reasoning_content": "original reasoning"}]}
    (old / "sessions/chat.json").write_text(json.dumps(document))
    (old / "memory").mkdir()
    (old / "memory/items.json").write_text(json.dumps([{"id": "mem1", "content": "user prefers cedar", "source_ref": "cli:old:0", "memory_type": "preference"}]))
    (old / "memory/PENDING.md").write_text("# Pending\n- original pending note\n")
    (old / "skills/local").mkdir(parents=True)
    (old / "skills/local/SKILL.md").write_text("---\nname: local\ndescription: local task\n---\noriginal skill\n")
    with sqlite3.connect(old / "sessions.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE messages(id TEXT,session_key TEXT,seq INTEGER,ts TEXT,role TEXT,content TEXT)")
        db.execute("INSERT INTO messages VALUES('sql0','web:old',0,'2026-07-20T12:00:00+00:00','user','SQL original')")
        db.commit()
        report = migrate(old, new)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert report["messages"] == 3 and report["sessions"] == 2
    assert report["history"]["json_added"] == 2
    assert report["active_memory_items"] == 1 and report["skills"] == 1
    assert "user prefers cedar" in (new / "memory/MEMORY.md").read_text()
    assert (new / "memory/VEDA.md").read_text() == "Kirakira persona\n"
    assert not (new / "memory/PENDING.md").exists()
    assert "original pending note" in (new / "memory/topics/legacy-pending.md").read_text()
    log = MessageLog(new / "sessions.db")
    try:
        rows = log.reader("akashic:legacy:cli:old").snapshot()
        assert [r.message_id for r in rows] == ["cli:old:0", "cli:old:1"]
        assert rows[1].body.parts[1].value["extra"] == json.dumps({"reasoning_content": "original reasoning"})
        assert log.owner("plugin:working_directory").read("directory:akashic:legacy:cli:old").value["path"] == str(old)
        with sqlite3.connect(new / "sessions.db") as db:
            metadata = json.loads(db.execute("SELECT metadata FROM sessions WHERE key='akashic:legacy:cli:old'").fetchone()[0])
            assert metadata["legacy_metadata"] == {"turn_count": 1}
    finally:
        log.close()
    activate(old / "config.toml", new)
    assert (old / "config.toml").read_text() == (new / "config.toml").read_text()
    assert "max_iterations" in (new / "legacy/config.toml").read_text()
    with pytest.raises(ValueError, match="changed after snapshot"):
        activate(old / "config.toml", new)


def test_cutover_refuses_messages_committed_after_the_import(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    legacy_workspace(old)
    migrate(old, new)
    original_config = (old / "config.toml").read_bytes()
    with sqlite3.connect(old / "sessions.db") as db:
        db.execute("UPDATE ledger_messages SET metadata_json=? WHERE message_id='m0'", ('{"new_fact":true}',))
    with pytest.raises(ValueError, match="data changed after snapshot"):
        activate(old / "config.toml", new)
    assert (old / "config.toml").read_bytes() == original_config
    assert (new / "legacy/sessions.db").exists()


@pytest.mark.parametrize("failure", ["conflicting-history", "unsupported-state", "active-owner"])
def test_migration_refuses_loss_or_live_workspace(tmp_path, failure):
    from bootstrap.workspace_lock import WorkspaceInstanceLock
    old, new = tmp_path / "old", tmp_path / "new"
    legacy_workspace(old)
    lock = None
    if failure == "conflicting-history":
        (old / "sessions").mkdir()
        (old / "sessions/chat.json").write_text(json.dumps({"key": "akashic:legacy", "messages": [
            {"role": "user", "content": "different", "timestamp": "2026-09-01T12:30:00+00:00"}]}))
    elif failure == "unsupported-state":
        (old / "mcp").mkdir()
        (old / "mcp/server.toml").write_text("legacy declaration")
    else:
        lock = WorkspaceInstanceLock(old)
        lock.acquire()
    try:
        with pytest.raises((ValueError, RuntimeError)):
            migrate(old, new)
        assert not new.exists()
        assert not list(tmp_path.glob(".kirakira-migration-*"))
    finally:
        if lock is not None:
            lock.release()


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


@pytest.mark.asyncio
async def test_web_branding_keeps_existing_account_identity_and_password_rotation(tmp_path):
    """A renamed default must not lock out an existing workspace owner."""
    import httpx

    from plugins.web_auth.app import create_app
    from plugins.web_auth.state import OwnerAuth

    auth = OwnerAuth(tmp_path / "auth")
    initial = auth.ensure()
    assert auth.check_password("kirakira", initial)
    account_path = auth.root / "account.json"
    account = json.loads(account_path.read_text())
    account["username"] = "existing-owner"
    account_path.write_text(json.dumps(account))
    assert auth.check_password("existing-owner", initial)
    assert not auth.check_password("kirakira", initial)
    auth.set_password("a-new-owner-password", keep=None)
    assert auth.username() == "existing-owner"
    assert auth.check_password("existing-owner", "a-new-owner-password")
    page = Path(__file__).parents[1] / "plugins/web_auth/login.html"
    app = create_app(auth, page)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
        response = await client.get("/_auth/login")
        assert 'value="existing-owner"' in response.text
        assert "__OWNER_USERNAME__" not in response.text
        token = auth.create_session(remember=False, label="test")
        from plugins.web_auth.state import COOKIE
        client.cookies.set(COOKIE, token)
        result = await client.post("/_auth/password", headers={"origin": "http://testserver", "x-akashic-host": "testserver"},
                                   json={"current": "a-new-owner-password", "password": "rotated-owner-password"})
        assert result.status_code == 200
        assert auth.check_password("existing-owner", "rotated-owner-password")


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


def test_frozen_memory_corpus_preserves_duplicate_sources_without_labels(tmp_path):
    from eval.memory_corpus import import_history

    rows = [{"role": "user", "content": "original source", "has_answer": True},
            {"role": "assistant", "content": "original reply"}]
    case = {"question_id": "fixed", "answer": "GOLD_NEVER_INDEXED", "answer_session_ids": ["source"],
            "haystack_session_ids": ["source", "source"],
            "haystack_dates": ["2023/05/23 (Tue) 05:09", "2023/05/24 (Wed) 05:53"],
            "haystack_sessions": [rows, rows]}
    directory = tmp_path / "history"
    messages, gold, sessions = import_history(case, directory)
    assert len(messages) == 4 and len({m.message_id for m in messages}) == 4
    assert len({m.session_id for m in messages}) == 2 and len(gold) == 2 and sessions == {"source"}
    assert messages[-1].metadata["evaluation_session_id"] == "source"
    with sqlite3.connect(directory / "sessions.db") as db:
        body = json.dumps(db.execute("SELECT body,metadata FROM messages").fetchall())
        assert "GOLD_NEVER_INDEXED" not in body and "has_answer" not in body
    assert import_history(case, directory, existing=True)[0] == messages
