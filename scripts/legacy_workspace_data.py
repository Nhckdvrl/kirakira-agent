"""Offline readers for the retired Kirakira workspace formats.

Only the migration command uses these readers. The running agent has no legacy
storage adapter. Copies containing personal data remain inside the private archive.
"""
import hashlib
import json
import shutil
import sqlite3
import tomllib
from contextlib import closing

from agent.plugin_composition.config_input import save_config
from plugins.ledger.contract import ContentPart, Input, Output
from plugins.markdown_memory.documents import MEMORY_HEADINGS, SELF_HEADINGS


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def database_digest(path):
    # SQLite backup normalizes committed WAL pages. A checkpoint alone must not
    # look like a data change, while committed changes must invalidate cutover.
    with (closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original,
          closing(sqlite3.connect(":memory:")) as copy):
        original.backup(copy)
        return hashlib.sha256(copy.serialize()).hexdigest()


def data_files(source):
    for name in (
        "sessions.db", "config.toml", "sessions", "memory", "skills", "uploads",
        "mcp", "proactive", "proactive.db", "drift", "observe", "coremem.db",
        "PROACTIVE_CONTEXT.md", ".kirakira/manifest.toml", ".kirakira/plugins",
        ".kirakira/plugin-data",
    ):
        entry = source / name
        if not entry.exists():
            continue
        for path in ([entry] if entry.is_file() else sorted(entry.rglob("*"))):
            if path.is_symlink():
                raise ValueError(f"legacy data contains a symlink: {path.relative_to(source)}")
            if path.is_file() and not path.name.endswith(("-wal", "-shm")):
                yield path


def verify_snapshot(source, archive):
    entries = json.loads((archive / "manifest.json").read_text())
    if {str(p.relative_to(source)) for p in data_files(source)} != {e["path"] for e in entries}:
        raise ValueError("source data files changed after snapshot")
    for entry in entries:
        path = source / entry["path"]
        current = database_digest(path) if entry["sqlite_snapshot"] else digest(path)
        if current != entry["source_sha256"]:
            raise ValueError("source data changed after snapshot")


def snapshot(source, archive):
    """Back up SQLite through its backup API, including committed WAL pages."""
    files = []
    for path in data_files(source):
        relative = path.relative_to(source)
        target = archive / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("rb") as stream:
            database = stream.read(16) == b"SQLite format 3\x00"
        original_digest = database_digest(path) if database else digest(path)
        if database:
            with (closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original,
                  closing(sqlite3.connect(target)) as backup):
                original.backup(backup)
        else:
            shutil.copyfile(path, target)
        target.chmod(0o700 if path.stat().st_mode & 0o111 else 0o600)
        files.append({"path": str(relative), "sha256": digest(target), "sqlite_snapshot": database,
                      "source_sha256": original_digest})
    (archive / "manifest.json").write_text(json.dumps(files, indent=2) + "\n")
    (archive / "manifest.json").chmod(0o600)
    verify_snapshot(source, archive)
    return files


def check_supported_state(archive):
    """Fail before publication when nonempty state needs another converter."""
    for name in ("proactive", "proactive.db", "drift", "observe", "mcp", "coremem.db",
                 ".kirakira/manifest.toml", ".kirakira/plugins", ".kirakira/plugin-data"):
        path = archive / name
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValueError(f"nonempty legacy {name} requires an explicit converter; original preserved")
    for path in (archive / "memory").glob("*.db"):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if path.name != "memory2.db" or "memory_items" not in tables:
                raise ValueError("legacy graph snapshot requires an explicit embedding-space converter")
            if any(db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                   for name in ("memory_items", "consolidation_events", "memory_replacements") if name in tables):
                raise ValueError("nonempty legacy vector memory requires an explicit converter")


def read_history(archive):
    """Merge SQL, JSON and the old search index; reject conflicting duplicates."""
    with closing(sqlite3.connect(archive / "sessions.db")) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "owner_records" in tables and db.execute("SELECT count(*) FROM owner_records").fetchone()[0]:
            raise ValueError("legacy owner records need an explicit state converter")
        canonical = "ledger_messages" in tables
        table, order = ("ledger_messages", "session_id,seq") if canonical else ("messages", "session_key,seq")
        rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        sessions = [dict(r) for r in db.execute("SELECT * FROM sessions")] if "sessions" in tables else []
    by_position = {(r["session_id" if canonical else "session_key"], r["seq"]): r for r in rows}
    counts = {"sqlite": len(rows), "json_added": 0, "index_added": 0, "duplicates": 0}
    session_info = {r["key"]: r for r in sessions}

    def merge(row, origin):
        position = (row["session_key"], row["seq"])
        previous = by_position.get(position)
        if previous is not None:
            # Canonical typed bodies cannot be compared to an obsolete rendered
            # transcript. Refuse ambiguity instead of throwing either record away.
            if canonical or any(previous.get(k) != row[k] for k in ("role", "content", "ts")):
                raise ValueError("conflicting legacy history at the same session/sequence")
            counts["duplicates"] += 1
            return
        if canonical:
            raise ValueError("mixed canonical and unimported legacy history needs reconciliation")
        rows.append(row)
        by_position[position] = row
        counts[origin + "_added"] += 1

    for path in sorted((archive / "sessions").glob("*.json")):
        document = json.loads(path.read_text())
        session = document["key"]
        session_info.setdefault(session, document)
        for seq, message in enumerate(document["messages"]):
            extra = {k: v for k, v in message.items() if k not in {"role", "content", "timestamp"}}
            merge({"id": f"{session}:{seq}", "session_key": session, "seq": seq,
                   "role": message["role"], "content": message.get("content"),
                   "ts": message["timestamp"], "extra_json": json.dumps(extra, ensure_ascii=False)}, "json")
    index = archive / "sessions/message_index.sqlite3"
    if index.exists():
        with closing(sqlite3.connect(index)) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT source_ref,session_key,role,content,timestamp FROM message_fts"):
                merge({"id": row["source_ref"], "session_key": row["session_key"],
                       "seq": int(row["source_ref"].rsplit(":", 1)[1]), "role": row["role"],
                       "content": row["content"], "ts": row["timestamp"]}, "index")
    return rows, canonical, session_info, counts


def legacy_body(row):
    if row["role"] not in {"user", "assistant"}:
        raise ValueError("legacy non-conversation record requires an explicit body converter")
    extra = row.get("extra_json")
    parts = (ContentPart("text", row["content"] or ""), ContentPart("history.provenance", {
        "schema": "sessions.messages.v0", "role": row["role"],
        "content_was_null": row["content"] is None, "extra": extra,
        "extra_sha256": hashlib.sha256(extra.encode()).hexdigest() if extra is not None else None,
    }))
    return Input(parts) if row["role"] == "user" else Output(parts, "complete")


def migrate_files(archive, staging, source):
    memory = staging / "memory"
    memory.mkdir(exist_ok=True)
    old_memory = archive / "memory"
    items_path = old_memory / "items.json"
    items = json.loads(items_path.read_text()) if items_path.exists() else []
    sections = {heading: [] for heading in MEMORY_HEADINGS[1:]}
    for item in items:
        if item.get("status", "active") != "active":
            continue
        heading = MEMORY_HEADINGS[2] if item.get("memory_type") == "preference" else MEMORY_HEADINGS[1]
        sections[heading].append("- " + item["content"].replace("\n", " "))
    (memory / "MEMORY.md").write_text(MEMORY_HEADINGS[0] + "\n\n" + "\n\n".join(
        heading + "\n" + "\n".join(sections[heading]) for heading in MEMORY_HEADINGS[1:]) + "\n")
    (memory / "SELF.md").write_text(SELF_HEADINGS[0] + "\n\n" + "\n\n".join((
        SELF_HEADINGS[1] + "\n- 我是 Kirakira，延续既有工作区中的人格和协作关系。",
        SELF_HEADINGS[2], SELF_HEADINGS[3])) + "\n")
    topics = memory / "topics"
    topics.mkdir(exist_ok=True)
    for path in sorted(old_memory.rglob("*.md")) if old_memory.exists() else ():
        relative = path.relative_to(old_memory)
        # New native profiles have a strict heading schema. Preserve complete
        # old documents as searchable topics, not malformed active profiles.
        name = "legacy-" + "-".join(relative.with_suffix("").parts).lower() + ".md"
        (topics / name).write_text('---\ndescription: "迁移前的 ' + str(relative) + ' 原文"\n---\n' + path.read_text())
    if items_path.exists():
        shutil.copyfile(items_path, memory / "legacy-items.json")
    skills = archive / "skills"
    if skills.exists():
        shutil.copytree(skills, staging / "skills")
    if (archive / "uploads").exists():
        shutil.copytree(archive / "uploads", staging / "uploads")
    config_path = archive / "config.toml"
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    for name, channel in config.get("channels", {}).items():
        if channel.get("enabled"):
            raise ValueError(f"enabled legacy channel {name} needs an explicit channel converter")
    agent = config.get("agent", {})
    if prompt := agent.get("system_prompt"):
        (memory / "VEDA.md").write_text(prompt + "\n")
    if agent:
        save_config(staging / "plugin-data/reply-builtin", {
            "max_steps": agent.get("max_iterations", 40),
            "max_output_tokens": agent.get("max_tokens"),
        })
    return {"memory_items": len(items), "active_memory_items": sum(len(v) for v in sections.values()),
            "markdown_documents": len(list(topics.glob("*.md"))),
            "skills": len(list((staging / "skills").glob("*/SKILL.md"))),
            "channels": {name: "disabled" for name in config.get("channels", {})},
            "legacy_model_replaced_by": "step5 on Step Token Plan",
            "working_directory": str(source)}
