"""Recovery uses committed sources, including missing notifications and crashes."""

import asyncio
import sqlite3
from unittest.mock import patch

import pytest

from session.manager import SessionManager
from tests.test_memory_commit_cycle import close, commit, engine_at, logical


def cursor(engine):
    return engine._store.db.execute("SELECT position FROM akasha_source_cursor").fetchone()[0]


def test_restart_catches_committed_turn_without_any_notification(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        commit(sessions, 1)  # Simulate crash between Session commit and fanout.
        close(engine)
        engine = engine_at(tmp_path)
        assert await engine.catch_up() == 1
        assert cursor(engine) == 1
        before = logical(engine._store)
        assert await engine.catch_up() == 0
        assert logical(engine._store) == before
        close(engine)
        sessions.close()
    asyncio.run(scenario())


def test_failed_predecessor_blocks_later_notification_and_cursor(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        first = commit(sessions, 1)
        second = commit(sessions, 2)
        with patch.object(engine._embedder, "embed_batch", side_effect=OSError("offline")):
            with pytest.raises(OSError):
                await engine._on_turn_committed(second)
        assert cursor(engine) == 0
        assert not engine._store.list_nodes()
        await engine._on_turn_committed(first)
        assert cursor(engine) == 2
        assert len(engine._store.list_nodes()) == 2
        close(engine)
        sessions.close()
    asyncio.run(scenario())


def test_first_activation_does_not_relearn_old_history_and_skips_are_audited(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        commit(sessions, 1)
        engine = engine_at(tmp_path)
        assert cursor(engine) == 1
        assert await engine.catch_up() == 0
        assert not engine._store.list_nodes()
        commit(sessions, 2, skip_post_memory=True)
        commit(sessions, 3, key="scheduler:test")
        await engine.catch_up()
        assert cursor(engine) == 3
        assert engine._store.db.execute("SELECT count(*) FROM akasha_source_skips").fetchone()[0] == 2
        assert not engine._store.list_nodes()
        close(engine)
        sessions.close()
    asyncio.run(scenario())


def test_cancelled_embedding_does_not_advance_consumption(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        commit(sessions, 1)
        entered = asyncio.Event()

        async def blocked(_texts):
            entered.set()
            await asyncio.Future()

        with patch.object(engine._embedder, "embed_batch", side_effect=blocked):
            task = asyncio.create_task(engine.catch_up())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cursor(engine) == 0
        await engine.catch_up()
        assert cursor(engine) == 1
        close(engine)
        sessions.close()
    asyncio.run(scenario())


def test_graph_failure_keeps_source_cursor_for_retry(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        commit(sessions, 1)
        with patch.object(engine._store, "upsert_message_node", side_effect=OSError("disk")):
            with pytest.raises(OSError):
                await engine.catch_up()
        assert cursor(engine) == 0
        close(engine)
        engine = engine_at(tmp_path)
        with patch.object(engine._embedder, "embed_batch", side_effect=AssertionError("fixed vectors must survive")):
            await engine.catch_up()
        assert cursor(engine) == 1
        close(engine)
        sessions.close()
    asyncio.run(scenario())


def test_publication_and_source_message_share_one_transaction(tmp_path):
    sessions = SessionManager(tmp_path)
    sessions._index.execute("""CREATE TRIGGER fail_publication AFTER INSERT ON message_publications
        BEGIN SELECT RAISE(ABORT, 'simulated publication failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="publication failure"):
        commit(sessions, 1)
    assert sessions._index.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert sessions._index.execute("SELECT count(*) FROM message_publications").fetchone()[0] == 0
    sessions.close()


def test_rebuild_cursor_catches_messages_arriving_before_activation(tmp_path):
    from scripts.replay_akasha import rebuild

    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        await engine._on_turn_committed(commit(sessions, 1))
        candidate = tmp_path / "candidate.db"
        rebuild(tmp_path / "sessions.db", candidate, engine._config.memory.embedding.model)
        commit(sessions, 2)  # Arrived after the snapshot, before activating candidate.
        path = engine._store.db_path
        close(engine)
        candidate.replace(path)
        engine = engine_at(tmp_path)
        assert cursor(engine) == 1
        await engine.catch_up()
        assert cursor(engine) == 2
        assert len(engine._store.list_nodes()) == 2
        close(engine)
        sessions.close()
    asyncio.run(scenario())


def test_different_source_database_cannot_reuse_consumption_cursor(tmp_path):
    sessions = SessionManager(tmp_path)
    engine = engine_at(tmp_path)
    close(engine)
    sessions._index.execute("UPDATE message_publication_stream SET stream_id='different'")
    sessions._index.commit()
    with pytest.raises(ValueError, match="source stream changed"):
        engine_at(tmp_path)
    sessions.close()


def test_periodic_worker_catches_up_without_a_new_turn_and_cancels_cleanly(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        engine = engine_at(tmp_path)
        commit(sessions, 1)
        worker = asyncio.create_task(engine.run_maintenance())
        for _ in range(100):
            if cursor(engine) == 1:
                break
            await asyncio.sleep(0.01)
        assert cursor(engine) == 1
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        close(engine)
        sessions.close()
    asyncio.run(scenario())
