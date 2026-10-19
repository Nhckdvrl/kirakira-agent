from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import math
import pytest
from plugins.wake_proactive.pool import InterestScore, mass, shortlist
from plugins.wake_proactive.state import ProactiveStateStore
from proactive_v2.loop import ProactiveLoop


def test_decay_mass_and_diversity_are_separate():
    now = datetime.now(timezone.utc)
    score = InterestScore(0.8, 0.9, True, "explicit subscription")
    initial = score.initial()
    assert mass(initial, now, now + timedelta(hours=18)) == pytest.approx(
        initial * initial / 2
    )
    assert score.initial(timestamp_present=False) == pytest.approx(initial * 0.03)
    items = [
        {"item_id": str(i), "_source": src, "_pool_mass": value}
        for i, (src, value) in enumerate([("a", 2), ("a", 1.8), ("b", 1.5)])
    ]
    assert [i["item_id"] for i in shortlist(items, limit=3)] == ["0", "2", "1"]
    for value in [float("nan"), float("inf"), -1, 2, True]:
        with pytest.raises(ValueError):
            InterestScore(value, 0.9, True, "invalid")


@pytest.mark.asyncio
async def test_pool_scoring_survives_restart_and_scores_each_revision_once(tmp_path):
    now = datetime.now(timezone.utc)
    item = {
        "item_id": "s:1",
        "_source": "s",
        "event_id": "1",
        "title": "agent runtime",
        "published_at": now.isoformat(),
    }
    path = tmp_path / "proactive.db"
    store = ProactiveStateStore(path)
    store.ingest("content", [item], now)
    calls = []

    class Judge:
        async def score_content(self, items, context):
            calls.append([i["item_id"] for i in items])
            return {
                i["item_id"]: InterestScore(0.6, 0.8, True, "some interest")
                for i in items
            }

    loop = object.__new__(ProactiveLoop)
    loop._state = store
    loop._judge = Judge()
    loop._cfg = SimpleNamespace(
        model="step5",
        pool_half_life_hours=18,
        pool_threshold=1.5,
        content_limit=5,
        session_key="cli:x",
    )
    assert (
        await loop._content_pool(store.pending_content(now), now, "agent research")
        == []
    )
    assert calls == [["s:1"]] and store.pending_content(now) == []
    store.close()
    store = ProactiveStateStore(path)
    loop._state = store
    # An unchanged score is reused; no new arrival means ContentModule stays silent.
    await loop._content_pool([], now, "agent research")
    assert len(calls) == 1
    store.ingest("content", [{**item, "title": "updated agent runtime"}], now)
    await loop._content_pool(store.pending_content(now), now, "agent research")
    assert len(calls) == 2
    old = {**item, "_revision": 1}
    assert not store.save_interest_score(
        old, "stale", InterestScore(0.9, 0.9, True, "old result"), now
    )
    store.close()


@pytest.mark.asyncio
async def test_one_arrival_wave_cannot_repeatedly_wake_old_high_ranked_page(tmp_path):
    now = datetime.now(timezone.utc)
    store = ProactiveStateStore(tmp_path / "pool.db")
    items = [
        {
            "item_id": "s:" + str(i),
            "_source": "s",
            "event_id": str(i),
            "title": "runtime " + str(i),
            "published_at": now.isoformat(),
        }
        for i in range(10)
    ]
    store.ingest("content", items, now)

    class Judge:
        async def score_content(self, items, context):
            return {
                i["item_id"]: InterestScore(0.9, 0.9, True, "explicit interest")
                for i in items
            }

    loop = object.__new__(ProactiveLoop)
    loop._state = store
    loop._judge = Judge()
    loop._cfg = SimpleNamespace(
        model="step5",
        pool_half_life_hours=18,
        pool_threshold=1.5,
        content_limit=3,
        session_key="cli:x",
    )
    page = await loop._content_pool(store.pending_content(now), now, "runtime")
    assert len(page) == 3 and store.pending_content(now) == []
    assert len(store.unread("content")) == 10  # retained pool, not falsely consumed
    store.close()
