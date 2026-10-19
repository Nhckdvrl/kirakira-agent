"""Real SQLite/engine tests: interrupted reads, replay, retries and rollback."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bus.events_lifecycle import TurnCommitted
from core.memory.engine import MemoryQuery, MemoryScope
from plugins.akasha.config import AkashaConfig
from plugins.akasha.cycle import apply_committed_turn
from plugins.akasha.engine import AkashaMemoryEngine, _load_committed_turn_messages
from plugins.akasha.replay import ReplayMessage
from plugins.akasha.store import AkashaStore
from plugins.default_memory.config_models import Config
from plugins.ledger.manager import SessionManager

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class Embeddings:
    async def embed(self, text):
        return [1.0, 0.2 if "music" in text else 0.6, 0.1]

    async def embed_batch(self, texts):
        return [await self.embed(text) for text in texts]

    def close(self):
        pass


def engine_at(root):
    with patch("plugins.akasha.engine.Embedder", return_value=Embeddings()):
        return AkashaMemoryEngine(
            config=Config(model="fake"),
            akasha_config=AkashaConfig(),
            workspace=root,
            http_resources=SimpleNamespace(external_default=None),
        )


def commit(sessions, number, text="music", *, key="web:one", **extra):
    session = sessions.get_or_create(key)
    user = session.add_message(
        "user", text, timestamp=(NOW + timedelta(hours=number)).isoformat()
    )
    assistant = session.add_message(
        "assistant",
        "reply " + text,
        timestamp=(NOW + timedelta(hours=number, seconds=1)).isoformat(),
        **extra,
    )
    sessions.save(session)
    return TurnCommitted(
        session_key=key,
        channel="web",
        chat_id="one",
        user_content=text,
        assistant_reply=assistant["content"],
        user_message_id=user["id"],
        assistant_message_id=assistant["id"],
        extra=extra,
    )


def logical(store):
    return {
        "nodes": [
            tuple(row)
            for row in store.db.execute(
                "SELECT key,anchor_id,salience,strength,resource,recall_count,embedding,emb_count FROM akasha_nodes ORDER BY key"
            )
        ],
        "edges": [
            tuple(row)
            for row in store.db.execute(
                "SELECT * FROM akasha_edges ORDER BY src_key,dst_key"
            )
        ],
        "receipts": [
            tuple(row)
            for row in store.db.execute(
                "SELECT * FROM akasha_commits ORDER BY turn_key"
            )
        ],
    }


def close(engine):
    for resource in engine.closeables:
        resource.close()


def test_reads_never_learn_duplicate_commit_is_idempotent_and_replay_matches(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        first = commit(sessions, 1)
        await engine._on_turn_committed(first)
        initial = logical(engine._store)
        for intent in ("context", "answer", "interest"):
            await engine.query(
                MemoryQuery(
                    text="music",
                    intent=intent,
                    scope=MemoryScope(session_key="web:one"),
                    timestamp=NOW + timedelta(hours=2),
                )
            )
        assert logical(engine._store) == initial
        await engine._on_turn_committed(first)
        assert logical(engine._store) == initial
        # A restart between turns must not lose previous co-activation state.
        close(engine)
        engine = engine_at(tmp_path)
        events = [
            first,
            commit(sessions, 3, "more music"),
            commit(sessions, 4, "music festival", key="web:two"),
        ]
        for event in events[1:]:
            await engine._on_turn_committed(event)
        replay = AkashaStore(tmp_path / "replay.db")
        for event in events:
            messages = _load_committed_turn_messages(tmp_path / "sessions.db", event)
            items = [
                ReplayMessage(m, await Embeddings().embed(m.content)) for m in messages
            ]
            apply_committed_turn(
                store=replay,
                config=AkashaConfig(),
                source_db_path=tmp_path / "sessions.db",
                items=items,
                model=engine._config.memory.embedding.model,
                message_embeddings=engine._message_embeddings,
                message_turn_keys=engine._message_turn_keys,
            )
        assert logical(engine._store) == logical(replay)
        replay.close()
        close(engine)
        sessions.close()

    asyncio.run(scenario())


def test_failure_rolls_back_graph_and_receipt_and_retry_reuses_fixed_vectors(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        event = commit(sessions, 1)
        original = engine._store.upsert_message_node
        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            result = original(*args)
            if calls == 2:
                raise OSError("disk fault")
            return result

        with patch.object(
            engine._store, "upsert_message_node", side_effect=fail_second
        ):
            with pytest.raises(OSError):
                await engine._on_turn_committed(event)
        assert not engine._store.list_nodes()
        assert (
            engine._store.db.execute("SELECT count(*) FROM akasha_commits").fetchone()[
                0
            ]
            == 0
        )
        with patch.object(
            engine._embedder,
            "embed_batch",
            side_effect=AssertionError("must use fixed vectors"),
        ):
            await engine._on_turn_committed(event)
        assert len(engine._store.list_nodes()) == 1
        close(engine)
        sessions.close()

    asyncio.run(scenario())


def test_commit_identity_cannot_drift_to_latest_repeated_text(tmp_path):
    sessions = SessionManager(tmp_path)
    first = commit(sessions, 1)
    second = commit(sessions, 2)
    assert (
        _load_committed_turn_messages(tmp_path / "sessions.db", first)[0].id
        == first.user_message_id
    )
    assert (
        _load_committed_turn_messages(tmp_path / "sessions.db", second)[0].id
        == second.user_message_id
    )
    legacy = TurnCommitted(
        session_key="web:one",
        channel="web",
        chat_id="one",
        user_content="music",
        assistant_reply="reply music",
    )
    with pytest.raises(ValueError, match="ambiguous"):
        _load_committed_turn_messages(tmp_path / "sessions.db", legacy)
    excluded = commit(sessions, 3, skip_post_memory=True)
    assert not _load_committed_turn_messages(tmp_path / "sessions.db", excluded)
    sessions.close()


@pytest.mark.parametrize("vector", [[float("nan"), 1, 2], [0, 0, 0], [1, 2]])
def test_invalid_embedding_never_publishes_partial_graph(tmp_path, vector):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        event = commit(sessions, 1)
        with patch.object(
            engine._embedder, "embed_batch", return_value=[[1, 2, 3], vector]
        ):
            with pytest.raises(ValueError):
                await engine._on_turn_committed(event)
        assert not engine._store.list_nodes()
        close(engine)
        sessions.close()

    asyncio.run(scenario())


def test_rebuild_audits_before_publish_and_never_overwrites_source_or_output(tmp_path):
    from scripts.replay_akasha import rebuild

    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        event = commit(sessions, 1)
        output = tmp_path / "candidate.db"
        with pytest.raises(ValueError, match="missing fixed embedding"):
            rebuild(
                tmp_path / "sessions.db", output, engine._config.memory.embedding.model
            )
        assert not output.exists()
        await engine._on_turn_committed(event)
        source_before = sessions._index.execute(
            "SELECT count(*) FROM messages"
        ).fetchone()[0]
        report = rebuild(
            tmp_path / "sessions.db", output, engine._config.memory.embedding.model
        )
        assert report["learned_turns"] == 1
        rebuilt = AkashaStore(output, read_only=True)
        assert logical(rebuilt) == logical(engine._store)
        rebuilt.close()
        with pytest.raises(FileExistsError):
            rebuild(
                tmp_path / "sessions.db", output, engine._config.memory.embedding.model
            )
        assert (
            sessions._index.execute("SELECT count(*) FROM messages").fetchone()[0]
            == source_before
        )
        close(engine)
        sessions.close()

    asyncio.run(scenario())


def test_replay_lexical_index_cannot_observe_future_messages(tmp_path):
    from plugins.akasha.core import turn_key
    from plugins.akasha.cycle import causal_source

    sessions = SessionManager(tmp_path)
    past = commit(sessions, 1, "astronomy past")
    # More future hits than the FTS top-k: post-filtering top-k would lose past.
    for number in range(2, 20):
        commit(sessions, number, "astronomy future " * 5)
    message = _load_committed_turn_messages(tmp_path / "sessions.db", past)[0]
    key = turn_key(message.session_key, message.seq, message.role)[2]
    with causal_source(tmp_path / "sessions.db", {key}) as cursor:
        hits = cursor.execute(
            "SELECT content FROM messages_fts WHERE messages_fts MATCH 'astronomy' ORDER BY rank LIMIT 10"
        ).fetchall()
        assert len(hits) == 2
        assert all("future" not in row[0] for row in hits)
    sessions.close()


def test_learning_config_change_requires_explicit_rebuild(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        await engine._on_turn_committed(commit(sessions, 1))
        before = logical(engine._store)
        engine._akasha_config = replace(AkashaConfig(), cross_boost=99)
        with pytest.raises(ValueError, match="configuration changed"):
            await engine._on_turn_committed(commit(sessions, 2))
        assert logical(engine._store) == before
        close(engine)
        sessions.close()

    asyncio.run(scenario())


def test_frozen_event_tool_chain_preserves_explicit_reinforcement():
    from plugins.akasha.engine import _reinforce_boost_for_turn

    chain = ({"calls": [{"name": "reinforce_memory", "status": "success"}]},)
    assert _reinforce_boost_for_turn({}, chain) > 1


def test_tied_timestamp_publications_and_offline_rebuild_preserve_identical_order(
    tmp_path,
):
    from scripts.replay_akasha import rebuild

    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        for number in range(12):
            event = commit(sessions, 1, "music " + str(number))  # exact same wall time
            await engine._on_turn_committed(event)
        before = logical(engine._store)
        assert len(before["receipts"]) == 12
        model = engine._config.memory.embedding.model
        close(engine)
        sessions.close()
        result = rebuild(tmp_path / "sessions.db", tmp_path / "candidate.db", model)
        assert result["learned_turns"] == 12
        candidate = AkashaStore(tmp_path / "candidate.db")
        assert logical(candidate) == before
        candidate.close()

    asyncio.run(scenario())
