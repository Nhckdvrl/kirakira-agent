"""Real source + graph + atomic checkpoint tests; hash vectors test integrity only."""

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.memory.engine import MemoryQuery
from eval.longmemeval.offline_ab import DeterministicEmbedder
from plugins.akasha2.engine import Akasha2MemoryEngine
from plugins.akasha2.infrastructure.persistence import logical_state_sha256
from plugins.default_memory.config_models import (
    Config,
    MemoryConfig,
    MemoryEmbeddingConfig,
)
from plugins.ledger.manager import SessionManager


def engine(path, **kwargs):
    return Akasha2MemoryEngine(
        config=Config(
            model="test",
            memory=MemoryConfig(
                embedding=MemoryEmbeddingConfig(
                    model="offline-hash-v1", output_dimensionality=64
                )
            ),
        ),
        workspace=path,
        http_resources=SimpleNamespace(),
        embedder=DeterministicEmbedder(),
        **kwargs,
    )


def add(manager, session="test:1", text="I live in Nagoya"):
    s = manager.get_or_create(session)
    s.add_message("user", text, timestamp="2026-01-01T00:00:00+00:00")
    s.add_message(
        "assistant", "Recorded: " + text, timestamp="2026-01-01T00:00:01+00:00"
    )
    manager.save(s)


@pytest.mark.asyncio
async def test_queries_are_pure_and_checkpoint_restores_exactly(tmp_path):
    sessions = SessionManager(tmp_path)
    e = engine(tmp_path)
    for i, text in enumerate(
        ["I live in Nagoya", "I like quiet cafes", "My train leaves at nine"]
    ):
        add(sessions, "test:" + str(i), text)
        await e.catch_up()
    q = MemoryQuery(
        "Which city is my home?", timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc)
    )
    before = await e.query(q)
    await e.aclose()
    checksum = logical_state_sha256(tmp_path / "memory/akasha2.db")
    reopened = engine(tmp_path)
    assert await reopened.catch_up() == 0
    assert (await reopened.query(q)).records == before.records
    await reopened.query(q)
    await reopened.aclose()
    assert logical_state_sha256(tmp_path / "memory/akasha2.db") == checksum
    sessions.close()


@pytest.mark.asyncio
async def test_unpublished_tail_replays_to_same_logical_state(tmp_path):
    sessions = SessionManager(tmp_path)
    e = engine(tmp_path, checkpoint_every=2)
    add(sessions, text="I live in Nagoya")
    await e.catch_up()
    # Export expected state elsewhere without publishing the source checkpoint.
    original = e._path
    e._path = tmp_path / "expected.db"
    e._publish()
    expected = logical_state_sha256(e._path)
    e._path = original
    await e.aclose()
    crashed = engine(tmp_path, checkpoint_every=2)
    assert await crashed.catch_up() == 1
    await crashed.aclose()
    assert logical_state_sha256(original) == expected
    sessions.close()


@pytest.mark.asyncio
async def test_first_activation_cutover_and_source_corruption_fail_loud(tmp_path):
    sessions = SessionManager(tmp_path)
    add(sessions)
    e = engine(tmp_path)
    assert await e.catch_up() == 0
    add(sessions, "test:2", "I work in Kyoto")
    await e.catch_up()
    await e.aclose()
    with sqlite3.connect(tmp_path / "sessions.db") as db:
        db.execute(
            "UPDATE messages SET content='tampered' WHERE session_key='test:2' AND role='user'"
        )
    with pytest.raises(ValueError, match="original messages changed"):
        engine(tmp_path)
    sessions.close()


@pytest.mark.asyncio
async def test_publish_failure_poisoned_engine_does_not_continue(tmp_path, monkeypatch):
    import plugins.akasha2.engine as mod

    sessions = SessionManager(tmp_path)
    e = engine(tmp_path, checkpoint_every=1)
    add(sessions)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_memory_database", fail)
    with pytest.raises(OSError):
        await e.catch_up()
    with pytest.raises(RuntimeError, match="publication failed"):
        await e.catch_up()
    await e.aclose()
    sessions.close()


@pytest.mark.asyncio
async def test_offline_rebuild_uses_fixed_vectors_without_changing_source(tmp_path):
    import hashlib

    from plugins.ledger.embedding_store import MessageEmbeddingStore
    from scripts.replay_akasha2 import rebuild

    sessions = SessionManager(tmp_path)
    add(sessions)
    cache = MessageEmbeddingStore(tmp_path / "sessions.db")
    embedder = DeterministicEmbedder()
    for message in sessions.get_or_create("test:1").messages:
        cache.upsert(
            message_id=message["id"],
            content=message["content"],
            model=embedder.model_id,
            embedding=await embedder.embed(message["content"]),
        )
    cache.close()
    sessions.close()
    source = tmp_path / "sessions.db"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = await rebuild(
        source, tmp_path / "candidate.db", embedder.model_id, "offline://fixed"
    )
    assert result["learned_turns"] == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with pytest.raises(FileExistsError):
        await rebuild(
            source, tmp_path / "candidate.db", embedder.model_id, "offline://fixed"
        )
    with pytest.raises(ValueError, match="missing fixed embedding"):
        await rebuild(source, tmp_path / "bad.db", "different-model", "offline://fixed")
    assert not (tmp_path / "bad.db").exists()


@pytest.mark.asyncio
async def test_checkpoint_writer_is_exclusive(tmp_path):
    sessions = SessionManager(tmp_path)
    first = engine(tmp_path)
    with pytest.raises(RuntimeError, match="already has a writer"):
        engine(tmp_path)
    await first.aclose()
    sessions.close()
