"""O: an Alert keeps its EventMail identity without waiting for Content work."""
import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest

from agent.plugin_composition import CompositionRoot
from plugins.eventmail.plugin import _StoreIO, _WakeServices
from plugins.eventmail.store import EventMailStore
from plugins.wake._boundary import SemanticInterest
from plugins.wake.admission import Duties
from plugins.wake.api import DriftWakeServices
from plugins.wake.state import WakeState


def test_alert_can_retrieve_memory_without_gaining_investigation_or_write_tools():
    from pydantic import ValidationError

    from plugins.wake.request import Request

    value = {"flow_id": "a" * 32, "owner": "alert", "now": datetime.now(UTC), "timezone": "UTC",
             "target": {"channel": "local", "recipient": "owner", "session_id": "owner"},
             "sink": {"name": "local", "address": "owner", "binding_id": "sink"},
             "program_binding": "program", "tools": {"share_alert": "share"}, "snapshot_seq": 0,
             "alert_ref": {"source_id": "monitor", "event_id": "review", "mail_id": "mail"},
             "rules": "", "history": ""}
    assert Request(**value).tools == {"share_alert": "share"}  # Existing archived requests stay valid.
    memory = {"recall_memory": "memory", "read_memory_source": "source", "search_memory_source": "source-search"}
    assert Request(**(value | {"tools": value["tools"] | memory})).tools.keys() == {"share_alert", *memory}
    for extra in ("web_fetch", "shell", "write_file", "forget_memory"):
        with pytest.raises(ValidationError, match="工具集合与职责"):
            Request(**(value | {"tools": value["tools"] | memory | {extra: "forbidden"}}))


@pytest.mark.asyncio
async def test_due_alert_bypasses_running_content_score(tmp_path):
    mail = EventMailStore(tmp_path / "mail.db")
    mail.initialize()
    state = WakeState(tmp_path / "wake.db")
    root = CompositionRoot("wake-isolation")
    entered, release = asyncio.Event(), asyncio.Event()
    now = datetime.now(UTC)
    mail.submit("feed", "batch", [{"item_id": "one", "revision": "r1", "payload": {"title": "news"}}])

    class Interest:
        async def score(self, texts, *, cutoff):
            entered.set()
            await release.wait()
            return tuple(0.5 for _ in texts)

    class Drift:
        async def snapshot(self, now):
            return {"proposals": ()}

    duties = Duties(_WakeServices(mail, _StoreIO()), cast(DriftWakeServices, Drift()), state, cast(SemanticInterest, Interest()))

    async def maintain():
        async with root.context.runtime_scope():
            return await duties.maintain(now)

    job = asyncio.create_task(maintain())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        receipt = mail.report_alert(source_id="monitor", event_id="alert", payload={"message": "attention"},
                                    observed_at=now)
        async with root.context.runtime_scope():
            admitted = await asyncio.wait_for(duties.check(now), 1)
        assert admitted.owner == "alert"
        assert admitted.pool.due_count == 1 and admitted.pool.scored_count == 0
        assert mail.peek_alert(now)["mail_id"] == receipt["mail_id"]
        assert mail.alert_status("monitor", "alert") == "pending"
        assert not job.done()
    finally:
        release.set()
        await job
        await root.dispose()


def test_idle_withdrawal_invalidates_pending_but_preserves_an_accepted_wake(tmp_path):
    from plugins.drift.store import DriftStore

    store = DriftStore(tmp_path / "drift.sqlite3")
    store.initialize()
    now = datetime.now(UTC)
    receipt = store.propose("idle:pending", "r1", {"question": "grounded research"}, now)
    assert store.withdraw("idle:pending", "r1")
    assert store.proposal_status("idle:pending", "r1") == "invalidated"
    assert not store.select(receipt["ref"], {"session_id": "wake:a", "turn_id": "input:a"}, now)["selected"]
    assert not store.withdraw("idle:pending", "r1")
    receipt = store.propose("idle:accepted", "r1", {"question": "accepted research"}, now)
    assert store.select(receipt["ref"], {"session_id": "wake:b", "turn_id": "input:b"}, now)["selected"]
    assert not store.withdraw("idle:accepted", "r1")
    assert store.proposal_status("idle:accepted", "r1") == "selected"


def test_idle_evidence_separates_presented_history_from_user_goal_authority():
    from plugins.drift.producer import Plan, check_evidence

    def plan(decision, refs):
        return Plan(decision=decision, subject="research", question="check original material",
                    reason="traceable context", evidence_message_ids=refs)

    check_evidence(plan("propose", ("user-goal", "assistant-reply")),
                   {"user-goal", "assistant-reply"}, {"user-goal"})
    check_evidence(plan("skip", ("assistant-reply",)), {"assistant-reply"}, set())
    with pytest.raises(ValueError, match="用户历史"):
        check_evidence(plan("propose", ("assistant-reply",)), {"assistant-reply"}, set())
    with pytest.raises(ValueError, match="未呈现"):
        check_evidence(plan("skip", ("forged-source",)), {"user-goal"}, {"user-goal"})
    conversation = plan("propose", ("user-goal",)).model_copy(update={"kind": "conversation"})
    with pytest.raises(ValueError, match="尚未开启"):
        check_evidence(conversation, {"user-goal"}, {"user-goal"})
    check_evidence(conversation, {"user-goal"}, {"user-goal"}, allow_conversation=True)
    assert Plan(decision="skip", kind=None, reason="no new reason to reach out").kind is None
    assert Plan(decision="skip", kind="", reason="completed").kind is None
    assert Plan(decision="skip", kind=None, subject=None, question=None, evidence_message_ids=None, reason="completed").decision == "skip"
    with pytest.raises(ValueError):
        Plan(decision="propose", kind=None, subject="topic", question="continue?", reason="reason", evidence_message_ids=("user-goal",))


@pytest.mark.asyncio
async def test_pause_after_lost_handoff_ack_withdraws_only_unclaimed_proposals(tmp_path):
    from types import SimpleNamespace

    from plugins.drift.producer import Config, Producer, digest
    from plugins.drift.store import DriftStore
    from plugins.ledger.contract import OWNER_STATE
    from plugins.ledger.log import MessageLog

    log = MessageLog(tmp_path / "ledger.db")
    domain = DriftStore(tmp_path / "drift.db")
    domain.initialize()
    now = datetime.now(UTC)
    try:
        for claimed in (False, True):
            identity = "claimed" if claimed else "pending"
            owner = log.owner("drift_" + identity)
            ctx = SimpleNamespace(require=lambda key, owner=owner: SimpleNamespace(open=lambda _: owner)
                                  if key == OWNER_STATE else log.catalog())
            producer = Producer(ctx, domain, Config(context_session_id="owner"))
            receipt = domain.propose(identity, "r1", {"question": "already committed"}, now)
            if claimed:
                assert domain.select(receipt["ref"], {"session_id": "wake:a", "turn_id": "input:a"}, now)["selected"]
            value = {"run_id": identity, "status": "planned", "automatic": True,
                     "config": digest(Config(auto_enabled=True, context_session_id="owner").model_dump()),
                     "model_call_id": "original-call", "proposal": {"proposal_id": identity, "revision": "r1"}}
            owner.transact(lambda tx, value=value: (tx.save("run", value, expected_version=None),
                                      tx.save("initiative:active", {"key": "run"}, expected_version=None)))
            assert await producer.recover() is claimed
            saved = owner.read("run").value
            assert saved["model_call_id"] == "original-call"
            assert saved["status"] == ("offered" if claimed else "cancelled")
            assert domain.proposal_status(identity, "r1") == ("selected" if claimed else "invalidated")
    finally:
        log.close()
