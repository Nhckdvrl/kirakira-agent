"""Import a legacy Kirakira message history into a separate native workspace.

The original workspace is never written. A SQLite backup is the stable import
source and remains in the destination as the complete recovery record. Old tool
authorizations and in-flight programs are never promoted into native grants.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp

from kirakira_agent.runtime import initialize_workspace
from plugins.ledger.contract import (
    ContentPart,
    Input,
    Message,
    Output,
    ToolCall,
    ToolResult,
    decode_body,
    encode_body,
)
from plugins.ledger.log import MessageLog


def _stamp(value):
    result = datetime.fromisoformat(value)
    return (result if result.tzinfo is not None else result.replace(tzinfo=UTC)).isoformat()


def migrate(source, destination):
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
        initialize_workspace(staging)
        archive = staging / "legacy"
        archive.mkdir(mode=0o700)
        # backup includes committed WAL contents; every subsequent read uses the
        # same snapshot, so a live source cannot produce a mixed history.
        with (closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as original,
              closing(sqlite3.connect(archive / "sessions.db")) as backup):
            original.backup(backup)
        os.chmod(archive / "sessions.db", 0o600)
        if (source / "config.toml").is_file():
            shutil.copyfile(source / "config.toml", archive / "config.toml")
            os.chmod(archive / "config.toml", 0o600)
        with closing(sqlite3.connect(archive / "sessions.db")) as original:
            original.row_factory = sqlite3.Row
            tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "ledger_messages" in tables:
                rows = [dict(row) for row in original.execute("SELECT * FROM ledger_messages ORDER BY session_id,seq")]
                canonical = True
            else:
                rows = [dict(row) for row in original.execute("SELECT * FROM messages ORDER BY session_key,seq")]
                canonical = False
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
                body = Input((ContentPart("text", row["content"]),)) if author == "user" else Output((ContentPart("text", row["content"]),), "complete")
                metadata = {"legacy": {"extra": json.loads(row.get("extra_json") or "{}")}}
            # Imported history is readable, but no registered live source can
            # replay its unfinished effects or reconstruct a current grant.
            source_name = "legacy:" + origin
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
        with closing(sqlite3.connect(staging / "sessions.db")) as target, target:
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            for identity, session, seq, timestamp, author, origin, body, metadata in imported:
                target.execute("INSERT OR IGNORE INTO sessions(key,created_at,updated_at) VALUES(?,?,?)", (session, timestamp, timestamp))
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
        report = {"schema_version": 1, "source_snapshot_sha256": hashlib.sha256((archive / "sessions.db").read_bytes()).hexdigest(),
                  "messages": len(imported), "sessions": len({row[1] for row in imported}), "bodies": dict(counts),
                  "policy": "historical facts only; legacy programs, grants and memory graphs stay archived/in the original workspace"}
        (staging / "migration.json").write_text(json.dumps(report, indent=2) + "\n")
        (staging / "config.toml").write_text("[runtime]\nworkspace = " + json.dumps(str(destination)) + "\n")
        os.rename(staging, destination)
        return report
    except BaseException:
        shutil.rmtree(staging)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate(args.source, args.destination), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
