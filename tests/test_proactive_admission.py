"""Input supersession must never settle a decision against a newer world."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from bus.queue import MessageBus
from plugins.proactive_flow.admission import DecisionSuperseded
from plugins.proactive_flow.judge import Decision
from plugins.wake_proactive.sources import SourceRegistry
from plugins.wake_proactive.state import ProactiveStateStore
from proactive_v2.config import ProactiveConfig
from proactive_v2.loop import ProactiveLoop
from session.manager import SessionManager
from tests.test_proactive_decisions import NOW, Script


def test_revisions_survive_restart_and_old_result_cannot_consume_or_check_new_payload(tmp_path):
    path = tmp_path / "proactive.db"
    store = ProactiveStateStore(path)
    event = {"item_id": "s:1", "_source": "s", "event_id": "1", "title": "old"}
    store.ingest("content", [event], NOW)
    old = store.unread("content")
    store.record_content_check("web:one", old, {"status": "valid"}, NOW)
    assert not store.pending_content(NOW)
    assert store.ingest("content", [{**event, "title": "new"}], NOW) == ["s:1"]
    assert store.consume_snapshot(old, NOW, acknowledge=True) == []
    store.record_content_check("web:one", old, {"status": "valid"}, NOW)
    store.close()
    store = ProactiveStateStore(path)
    new = store.pending_content(NOW)
    assert new[0]["title"] == "new" and new[0]["_revision"] == 2
    assert not store.pending_acknowledgements()
    assert store._db.execute("SELECT count(*) FROM event_revisions").fetchone()[0] == 2
    assert store.ingest("content", [{**event, "title": "new"}], NOW) == []
    assert store.consume_snapshot(new, NOW, acknowledge=True) == ["s:1"]
    store.close()


@pytest.mark.parametrize("change", ["conversation", "busy", "revision", "rules"])
def test_decision_is_rejected_if_inputs_change_during_model_call(tmp_path, change):
    async def scenario():
        sessions = SessionManager(tmp_path)
        store = ProactiveStateStore(tmp_path / "proactive.db")
        event = {"item_id": "s:1", "_source": "s", "event_id": "1", "title": "music"}
        store.ingest("content", [event], NOW)
        busy = False
        loop = ProactiveLoop(config=ProactiveConfig(channel="web", chat_id="one"),
            bus=MessageBus(), session_manager=sessions, model_client=Script([]),
            sources=SourceRegistry(), state=store, passive_busy_fn=lambda _: busy)
        loop._deliver = AsyncMock(side_effect=AssertionError("stale decision reached delivery"))

        async def changed(*_args, **_kwargs):
            nonlocal busy
            if change == "conversation":
                session = sessions.get_or_create("web:one")
                session.add_message("user", "wait")
                sessions.save(session)
            elif change == "busy":
                busy = True
            elif change == "revision":
                store.ingest("content", [{**event, "title": "updated"}], NOW)
            else:
                (tmp_path / "PROACTIVE_CONTEXT.md").write_text("Do not share music")
            return Decision(True, "stale message", ["s:1"])

        loop._judge.decide_content = changed
        with pytest.raises(DecisionSuperseded):
            await loop._push_content(store.unread("content"), NOW, "", "", "", "")
        loop._deliver.assert_not_awaited()
        assert store.unread_count("content") == 1
        assert store.pending_content(NOW + timedelta(minutes=5))
        assert store.recent_decision_runs()[0]["decision"]["status"] == "superseded"
        store.close()
        sessions.close()
    asyncio.run(scenario())


def test_revision_arriving_during_delivery_is_not_settled_by_old_ack(tmp_path):
    async def scenario():
        sessions = SessionManager(tmp_path)
        store = ProactiveStateStore(tmp_path / "proactive.db")
        event = {"item_id": "s:1", "_source": "s", "event_id": "1", "title": "old"}
        store.ingest("content", [event], NOW)
        loop = ProactiveLoop(config=ProactiveConfig(channel="web", chat_id="one"),
            bus=MessageBus(), session_manager=sessions, model_client=Script([]),
            sources=SourceRegistry(), state=store)
        loop._judge.decide_content = AsyncMock(return_value=Decision(True, "old", ["s:1"]))

        async def send(*_args):
            store.ingest("content", [{**event, "title": "new"}], NOW)
            return True

        loop._deliver = send
        assert await loop._push_content(store.unread("content"), NOW, "", "", "", "")
        assert store.pending_content(NOW)[0]["title"] == "new"
        assert not store.pending_feedback()
        store.close()
        sessions.close()
    asyncio.run(scenario())
