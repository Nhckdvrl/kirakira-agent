"""Rebuild v1 learning into a NEW sidecar from a consistent, read-only snapshot.

No model/network calls, source writes, overwrite, or automatic activation.
All eligible embeddings are audited before the destination is created.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import numpy as np

from bus.events_lifecycle import TurnCommitted
from plugins.akasha.config import AkashaConfig, load_akasha_config
from plugins.akasha.core import turn_key
from plugins.akasha.cycle import apply_committed_turn, validate_turn
from plugins.akasha.engine import _load_committed_turn_messages, _reinforce_boost_for_turn
from plugins.akasha.replay import ReplayMessage
from plugins.akasha.store import AkashaStore


def rebuild(source: Path, output: Path, model: str, *, config: AkashaConfig | None = None) -> dict:
    if output.exists():
        raise FileExistsError("replay requires a new output path")
    config = config or AkashaConfig()
    with tempfile.TemporaryDirectory(prefix="kirakira-replay-") as temporary:
        snapshot = Path(temporary) / "sessions.db"
        with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(snapshot)) as copy:
                live.backup(copy)
        turns = []
        skipped = 0
        vectors: dict[str, np.ndarray] = {}
        keys: dict[str, str] = {}
        with closing(sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM messages WHERE role='assistant' ORDER BY session_key,seq").fetchall()
            for row in rows:
                extra = json.loads(row["extra_json"] or "{}")
                if row["session_key"].startswith("scheduler:"):
                    skipped += 1
                    continue
                event = TurnCommitted(session_key=row["session_key"], channel="replay", chat_id="",
                                      assistant_message_id=row["id"], extra=extra,
                                      tool_chain_raw=tuple(extra.get("tool_chain") or ()))
                messages = _load_committed_turn_messages(snapshot, event)
                if not messages:
                    skipped += 1
                    continue
                items = []
                for message in messages:
                    vector = db.execute(
                        "SELECT embedding,dim FROM message_embeddings WHERE message_id=? AND model=? AND content_hash=?",
                        (message.id, model, hashlib.sha256(message.content.encode()).hexdigest()),
                    ).fetchone()
                    if vector is None:
                        raise ValueError("missing fixed embedding for message " + message.id)
                    dense = np.frombuffer(vector["embedding"], dtype="<f4").copy()
                    if len(dense) != vector["dim"]:
                        raise ValueError("corrupt embedding for message " + message.id)
                    items.append(ReplayMessage(message, dense.tolist()))
                    vectors[message.id] = dense
                    keys[message.id] = turn_key(message.session_key, message.seq, message.role)[2]
                validate_turn(items)
                turns.append((items, _reinforce_boost_for_turn(event.extra, event.tool_chain_raw)))
        if len({len(item.embedding) for items, _ in turns for item in items}) > 1:
            raise ValueError("source mixes embedding dimensions")
        turns.sort(key=lambda pair: (datetime.fromisoformat(pair[0][1].message.ts),
                                    keys[pair[0][0].message.id]))
        # Publish only after complete replay, on the destination filesystem.
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".akasha-replay-", dir=output.parent) as staging:
            candidate = Path(staging) / "akasha.db"
            store = AkashaStore(candidate)
            try:
                for items, boost in turns:
                    apply_committed_turn(store=store, config=config, source_db_path=snapshot,
                        items=items, model=model, message_embeddings=vectors,
                        message_turn_keys=keys, reinforce_boost=boost)
                # Preserve the snapshot's cutover so messages arriving between
                # backup and activation are still consumed after activation.
                from plugins.akasha.consumption import initialize_cursor
                initialize_cursor(store, snapshot)
                receipt_rows = [tuple(row) for row in store.db.execute(
                    "SELECT turn_key,fingerprint FROM akasha_commits ORDER BY turn_key")]
            finally:
                store.close()
            with candidate.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(candidate, output)  # atomic no-replace, even if another writer races
            directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return {"learned_turns": len(turns), "skipped_assistant_messages": skipped,
            "model": model, "output": str(output),
            "input_digest": hashlib.sha256(json.dumps(receipt_rows).encode()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Exact model identity of stored message embeddings")
    parser.add_argument("--config-dir", type=Path,
                        help="Directory containing Akasha config.local.toml; otherwise use default settings")
    args = parser.parse_args()
    if args.config_dir is not None and not (args.config_dir / "config.local.toml").is_file():
        parser.error("--config-dir must contain config.local.toml")
    config = load_akasha_config(plugin_dir=args.config_dir) if args.config_dir else AkashaConfig()
    print(json.dumps(rebuild(args.source, args.output, args.model, config=config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
