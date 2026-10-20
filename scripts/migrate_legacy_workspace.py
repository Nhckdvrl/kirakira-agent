"""Migrate legacy Kirakira data and activate a separate native workspace.

The original data is never written. Lifecycle locks exclude a running owner.
A SQLite backup is the stable import
source and remains in the destination as the complete recovery record. Old tool
authorizations and in-flight programs are never promoted into native grants.
"""
import argparse
import asyncio
import json
import os
import shutil
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp

from bootstrap.workspace_lock import WorkspaceInstanceLock
from kirakira_agent.runtime import initialize_workspace
from plugins.ledger.contract import (
    ContentPart,
    Message,
    Output,
    ToolCall,
    ToolResult,
    decode_body,
    encode_body,
)
from plugins.ledger.log import MessageLog, decode_attributes, encode_attributes
from scripts.legacy_workspace_data import (
    check_supported_state,
    digest,
    legacy_body,
    migrate_files,
    read_history,
    snapshot,
    verify_snapshot,
)


def _stamp(value):
    result = datetime.fromisoformat(value)
    return (result if result.tzinfo is not None else result.replace(tzinfo=UTC)).isoformat()


def migrate(source, destination, *, profile="web"):
    source = Path(source).resolve()
    # Fence the actual data writer. A Supervisor's management lease is distinct
    # and may survive on another NFS client while no Runtime is running.
    lock = WorkspaceInstanceLock(source)
    lock.acquire()
    try:
        return _migrate_locked(source, destination, profile=profile)
    finally:
        lock.release()


def _migrate_locked(source, destination, *, profile):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("source and destination must be separate workspace trees")
    if destination.exists():
        raise FileExistsError("destination must not exist")
    database = source / "sessions.db"
    if not database.is_file():
        raise FileNotFoundError("legacy sessions.db is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=".kirakira-migration-", dir=destination.parent))
    try:
        staging.chmod(0o700)
        initialize_workspace(staging, profile=profile)
        archive = staging / "legacy"
        archive.mkdir(mode=0o700)
        files = snapshot(source, archive)
        check_supported_state(archive)
        rows, canonical, session_info, history_counts = read_history(archive)
        file_counts = migrate_files(archive, staging, source)
        log = MessageLog(staging / "sessions.db")
        log.close()
        counts = defaultdict(int)
        imported = []
        for row in rows:
            if canonical:
                body = decode_body(row["body_json"])
                session, seq, identity = row["session_id"], row["seq"], row["message_id"]
                timestamp, author, origin = _stamp(row["recorded_at"]), row["author"], row["source"]
                metadata = json.loads(row["metadata_json"])
            else:
                session, seq, identity = row["session_key"], row["seq"], row["id"]
                timestamp, author, origin = _stamp(row["ts"]), row["role"], "conversation"
                body = legacy_body(row)
                metadata = {"legacy": {"extra": json.loads(row.get("extra_json") or "{}")}}
            # Imported history is readable, but no registered live source can
            # replay its unfinished effects or reconstruct a current grant.
            source_name = "legacy:" + origin if canonical else "legacy-unattributed"
            original_session = session
            session = session if session.startswith("akashic:") else "akashic:legacy:" + session
            metadata = dict(metadata)
            metadata["legacy_session_id"] = original_session
            Message(identity, session, seq, datetime.fromisoformat(timestamp), author,
                    source_name, body, metadata)
            imported.append((identity, session, seq, timestamp, author, source_name, body, metadata))
            counts[type(body).__name__] += 1
        references = {row[0]: row for row in imported}
        seen_results = set()
        for row in imported:
            body = row[6]
            if isinstance(body, ToolResult):
                ref = body.call_ref
                parent = references.get(ref.message_id)
                if (parent is None or parent[1] != row[1] or parent[5] != row[5]
                        or parent[2] >= row[2] or not isinstance(parent[6], Output)
                        or ref.part_index >= len(parent[6].parts)
                        or not isinstance(parent[6].parts[ref.part_index], ToolCall)
                        or ref in seen_results):
                    raise ValueError("legacy tool result does not match one earlier call")
                seen_results.add(ref)
        session_map = {old: old if old.startswith("akashic:") else "akashic:legacy:" + old
                       for old in {*session_info, *(row["session_id" if canonical else "session_key"] for row in rows)}}
        if len(set(session_map.values())) != len(session_map):
            raise ValueError("legacy session address collision")
        with closing(sqlite3.connect(staging / "sessions.db")) as target, target:
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            for old, new in session_map.items():
                info = session_info.get(old, {})
                timestamp = _stamp(info.get("created_at") or next((r[3] for r in imported if r[1] == new), datetime.now(UTC).isoformat()))
                raw_attributes = info.get("attributes", '{"visibility":"listed","learning":"eligible"}')
                attributes = decode_attributes(raw_attributes)
                if attributes.scope:
                    raise ValueError("legacy scoped session needs its project/state converter")
                raw_metadata = info.get("metadata_json", info.get("metadata", {})) or {}
                original_metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
                target.execute("INSERT INTO sessions(key,created_at,updated_at,metadata,attributes,title) VALUES(?,?,?,?,?,?)",
                               (new, timestamp, _stamp(info.get("updated_at", timestamp)),
                                json.dumps({"legacy_session_id": old, "legacy_metadata": original_metadata}),
                                encode_attributes(attributes), info.get("title")))
                target.execute("INSERT INTO owner_records VALUES(?,?,?,?)", ("plugin:working_directory", "directory:" + new,
                               1, json.dumps({"path": str(source)})))
            for identity, session, seq, timestamp, author, origin, body, metadata in imported:
                target.execute("INSERT INTO messages(id,session_key,seq,ts,author,source,body,metadata) VALUES(?,?,?,?,?,?,?,?)",
                               (identity, session, seq, timestamp, author, origin, encode_body(body), json.dumps(metadata)))
                if isinstance(body, Output):
                    for part in body.parts:
                        if isinstance(part, ToolCall):
                            descriptor = json.dumps({"version": 2, "origins": {"legacy-import": "archived"},
                                "service": "tools.bind-saved.v1", "metadata": {
                                    "tool": {"name": part.binding_id, "owner": "legacy-import"},
                                    "legacy_import": True}})
                            target.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (part.binding_id, descriptor))
                            target.execute("INSERT OR IGNORE INTO message_bindings VALUES(?,?)", (identity, part.binding_id))
                target.execute("UPDATE sessions SET next_seq=max(next_seq,?),created_at=min(created_at,?),updated_at=max(updated_at,?) WHERE key=?", (seq + 1, timestamp, timestamp, session))
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("native import failed foreign key validation")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("native import failed integrity validation")
        reopened = MessageLog(staging / "sessions.db")
        try:
            for row in imported:
                actual = reopened.reader(row[1]).get(row[0])
                if actual is None or actual.seq != row[2] or encode_body(actual.body) != encode_body(row[6]):
                    raise ValueError("native import readback mismatch")
        finally:
            reopened.close()
        from agent.plugin_composition.config_input import save_config
        learning_sources = sorted({"conversation", "programmatic", "legacy-unattributed", *(row[5] for row in imported)})
        for plugin in ("akasha", "markdown_memory"):
            save_config(staging / f"plugin-data/{plugin}-builtin", {"sources": learning_sources})
        for entry in files:
            if digest(archive / entry["path"]) != entry["sha256"]:
                raise ValueError("archive verification failed")
        report = {"schema_version": 2, "source": str(source), "destination": str(destination), "profile": profile,
                  "source_snapshot_sha256": digest(archive / "sessions.db"),
                  "archived_files": len(files), "messages": len(imported), "sessions": len(session_map),
                  "session_map": session_map, "bodies": dict(counts), "history": history_counts, **file_counts,
                  "policy": "native history, profiles, skills and directory snapshots; no imported execution grants",
                  "model_status": "requires model configuration and verification"}
        (staging / "migration.json").write_text(json.dumps(report, indent=2) + "\n")
        (staging / "config.toml").write_text("[runtime]\nworkspace = " + json.dumps(str(destination)) + "\n")
        os.rename(staging, destination)
        return report
    except BaseException:
        shutil.rmtree(staging)
        raise


def activate(config_path, workspace):
    """Compare the saved source config before publishing the new entry point."""
    config_path, workspace = Path(config_path).resolve(), Path(workspace).resolve()
    saved = workspace / "legacy/config.toml"
    if not saved.is_file() or not config_path.is_file() or digest(saved) != digest(config_path):
        raise ValueError("source configuration changed after snapshot; activation refused")
    report = json.loads((workspace / "migration.json").read_text())
    source = Path(report["source"])
    lock = WorkspaceInstanceLock(source)
    lock.acquire()
    temporary = config_path.with_name(config_path.name + ".native-cutover.tmp")
    try:
        verify_snapshot(source, workspace / "legacy")
        with temporary.open("x") as stream:
            temporary.chmod(0o600)
            stream.write((workspace / "config.toml").read_text())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
        lock.release()


async def configure_and_verify(workspace, key_file, *, model_config=None):
    from uuid import uuid4

    from eval.models import EvaluationModel
    from kirakira_agent.runtime import runtime, submit
    from plugins.ledger.contract import MESSAGE_CATALOG
    from plugins.models.contract import MODEL_CATALOG
    model_config = model_config or EvaluationModel.read(None)
    report_path = workspace / "migration.json"
    report = json.loads(report_path.read_text())
    os.environ.setdefault("AKASHIC_PLUGIN_HOME", os.environ.get(
        "KIRAKIRA_PLUGIN_HOME", str(Path.home() / ".kirakira/plugins")))
    async with runtime(workspace) as core:
        await model_config.configure(core, key_file)
        root = core.plugin_manager.live_root
        catalog = root.context.require(MESSAGE_CATALOG)
        for session in report["session_map"].values():
            _ = catalog.reader(session).snapshot()
        marker = "MIGRATION_" + uuid4().hex[:10]
        answer = await submit(core, "akashic:migration-check", "migration-" + uuid4().hex,
                              "Reply with exactly " + marker + ". Do not call tools.")
        text = "".join(part.value for part in answer.body.parts if isinstance(part, ContentPart) and part.kind == "text")
        if text.strip() != marker:
            raise ValueError("model migration verification failed")
        models = root.context.require(MODEL_CATALOG).snapshot()
        connection = models.connection(models.model(model_config.model_id).connection_id)
        with closing(sqlite3.connect((workspace / "model-registry.sqlite3").as_uri() + "?mode=ro", uri=True)) as database:
            endpoint = database.execute("SELECT base_url FROM model_connections WHERE id=?", (connection.connection_id,)).fetchone()[0]
        assert endpoint == model_config.endpoint
        report["verification"] = {"model_reply": True, "model_config": model_config.description(), "driver": connection.driver_id,
                                  "endpoint": endpoint, "legacy_sessions_readable": len(report["session_map"])}
    report["model_status"] = "configured and verified"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--profile", choices=("headless", "web"), default="web")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--activate-config", type=Path)
    parser.add_argument("--model-config", type=Path)
    args = parser.parse_args()
    if args.activate_config and not args.key_file:
        parser.error("--activate-config requires --key-file and successful model/runtime verification")
    report = migrate(args.source, args.destination, profile=args.profile)
    if args.key_file:
        from eval.models import EvaluationModel
        report = asyncio.run(configure_and_verify(args.destination.resolve(), args.key_file,
            model_config=EvaluationModel.read(args.model_config)))
    if args.activate_config:
        activate(args.activate_config, args.destination)
        report["activated"] = True
        (args.destination / "migration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
