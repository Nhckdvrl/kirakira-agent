"""Transactional v1 learning cycle shared by live commits and offline replay.

This deliberately retains v1 retrieval math. It adopts the recoverability
contracts of Akasha2 without claiming to implement its graph algorithm.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import sqlite3
from contextlib import closing, contextmanager

import numpy as np

from plugins.akasha.config import AkashaConfig
from plugins.akasha.core import AkashaCandidate, turn_key, document_idf, lexical_statistics
from plugins.akasha.replay import AkashaReplayRuntime, ReplayMessage
from plugins.akasha.store import AkashaStore


@contextmanager
def causal_source(source_db_path: Path, nodes: set[str]):
    """FTS/BM25 must see the learned prefix, not future rows in a replay file.

    This v1 bridge rebuilds a small in-memory index per commit. A large history
    should move to an incremental causal index (or Akasha2), measured first.
    """
    with closing(sqlite3.connect(f"{source_db_path.resolve().as_uri()}?mode=ro", uri=True)) as source:
        rows = [row for row in source.execute(
            "SELECT id,session_key,seq,role,content,ts FROM messages ORDER BY session_key,seq"
        ) if row[3] in {"user", "assistant"} and turn_key(row[1], row[2], row[3])[2] in nodes]
    with closing(sqlite3.connect(":memory:")) as prefix:
        prefix.execute("CREATE TABLE messages(id, session_key, seq, role, content, ts)")
        prefix.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", rows)
        prefix.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, tokenize='trigram')")
        prefix.execute("INSERT INTO messages_fts(rowid,content) SELECT rowid,content FROM messages")
        with lexical_statistics(document_idf(row[4] for row in rows)):
            yield prefix.cursor()


def validate_turn(items: list[ReplayMessage]) -> None:
    if len(items) != 2:
        raise ValueError("learning requires one complete persisted user/assistant pair")
    user, assistant = (item.message for item in items)
    if (user.role != "user" or assistant.role != "assistant"
            or user.session_key != assistant.session_key or assistant.seq != user.seq + 1
            or not user.id or not assistant.id or user.id == assistant.id):
        raise ValueError("invalid committed turn identity")
    for message in (user, assistant):
        if datetime.fromisoformat(message.ts).utcoffset() is None:
            raise ValueError("learning timestamps must include a timezone")
        if not message.content.strip():
            raise ValueError("learning requires nonempty message text")
    if datetime.fromisoformat(assistant.ts) < datetime.fromisoformat(user.ts):
        raise ValueError("assistant precedes user")
    dimension = len(items[0].embedding)
    for item in items:
        vector = np.asarray(item.embedding, dtype=np.float32)
        if (vector.ndim != 1 or not dimension or len(vector) != dimension
                or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0):
            raise ValueError("invalid or incompatible message embedding")


def apply_committed_turn(
    *, store: AkashaStore, config: AkashaConfig, source_db_path: Path,
    items: list[ReplayMessage], model: str,
    message_embeddings: dict[str, np.ndarray], message_turn_keys: dict[str, str],
    reinforce_boost: float = 1.0,
    source_position: int | None = None,
) -> bool:
    validate_turn(items)
    user, assistant = (item.message for item in items)
    key = turn_key(user.session_key, user.seq, user.role)[2]
    # Path and presentation budgets do not change learning; all algorithm
    # settings do. Refuse to silently mix configurations in one graph.
    learning_config = {name: value for name, value in asdict(config).items()
                       if name not in {"db_path", "inject_max_chars", "assistant_preview_chars"}}
    config_json = json.dumps(learning_config, sort_keys=True, allow_nan=False)
    payload = {"model": model, "config": learning_config, "items": [
        {"message": asdict(item.message), "embedding": np.asarray(item.embedding, dtype=np.float32).tolist()}
        for item in items
    ], "reinforce_boost": reinforce_boost}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                             allow_nan=False).encode()).hexdigest()
    with store.transaction():
        saved_config = store.db.execute("SELECT config_json FROM akasha_cycle_config").fetchone()
        if saved_config is not None and saved_config[0] != config_json:
            raise ValueError("learning configuration changed; explicit rebuild required")
        store.db.execute("INSERT OR IGNORE INTO akasha_cycle_config VALUES (1, ?)", (config_json,))
        prior = store.db.execute("SELECT fingerprint FROM akasha_commits WHERE turn_key=?", (key,)).fetchone()
        if prior is not None:
            if prior[0] != fingerprint:
                raise ValueError("committed memory input changed; explicit rebuild required")
            if source_position is not None:
                _advance_cursor(store, source_position)
            return False
        if store.db.execute("SELECT 1 FROM akasha_nodes WHERE key=?", (key,)).fetchone():
            raise ValueError("legacy turn has no commit receipt; rebuild to replay it")
        space = store.db.execute("SELECT model, dimension FROM akasha_learning_space").fetchone()
        identity = (model, len(items[0].embedding))
        if space is not None and tuple(space) != identity:
            raise ValueError("embedding space changed; explicit rebuild required")
        if any(node.embedding.size != identity[1] for node in store.list_nodes()):
            raise ValueError("embedding dimension differs from existing graph")
        store.db.execute("INSERT OR IGNORE INTO akasha_learning_space VALUES (1, ?, ?)", identity)
        latest = store.db.execute(
            "SELECT committed_at, turn_key FROM akasha_commits ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if latest:
            current_time, latest_time = datetime.fromisoformat(assistant.ts), datetime.fromisoformat(latest[0])
            if current_time < latest_time or (current_time == latest_time and source_position is None and key <= latest[1]):
                raise ValueError("out-of-order learning requires explicit replay")
            # Tied wall clocks are common in imports. The durable publication
            # position, rather than lexicographic seq text, owns live order.
        # Only the already-published prefix is visible to the shared algorithm.
        # Cached embeddings for future source messages must not become seeds.
        nodes = {node.key for node in store.list_nodes()}
        prefix = {mid: vector for mid, vector in message_embeddings.items()
                  if message_turn_keys.get(mid) in nodes}
        previous = store.db.execute(
            "SELECT activation_json FROM akasha_commits WHERE session_key=? ORDER BY rowid DESC LIMIT 1",
            (user.session_key,),
        ).fetchone()
        previous_activations = ({user.session_key: [
            AkashaCandidate(**row) for row in json.loads(previous[0])
        ]} if previous else {})
        with causal_source(source_db_path, nodes) as cursor:
            runtime = AkashaReplayRuntime(
                store=store, config=config, source_db_path=source_db_path,
                source_cursor=cursor, message_embeddings=prefix,
                message_turn_keys={mid: message_turn_keys[mid] for mid in prefix},
                reinforce_boosts={key: reinforce_boost},
                previous_activations=previous_activations,
            )
            result = runtime.replay_turn(items)
        store.db.execute("INSERT INTO akasha_commits VALUES (?, ?, ?, ?, ?)", (
            key, fingerprint, user.session_key, assistant.ts,
            json.dumps([asdict(item) for item in result.activation_items], sort_keys=True),
        ))
        if source_position is not None:
            _advance_cursor(store, source_position)
    return True


def _advance_cursor(store: AkashaStore, position: int) -> None:
    store.db.execute("UPDATE akasha_source_cursor SET position=? WHERE singleton=1 AND position < ?",
                     (position, position))
