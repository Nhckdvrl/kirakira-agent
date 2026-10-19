from datetime import datetime, timezone
import pytest
from plugins.drift_flow.state import DriftStateStore


def test_drift_proposal_identity_priority_staleness_and_recovery(tmp_path):
    path = tmp_path / "drift.db"
    state = DriftStateStore(path)
    now = datetime.now(timezone.utc)

    def propose(head, skill, priority=0):
        return state.propose(
            session_key="s",
            source_head=head,
            skill=skill,
            skill_digest="v1",
            briefing="task " + skill,
            reason="explicit idle work",
            priority=priority,
            now=now,
        )

    stale = propose((1, "a"), "old")
    normal = propose((2, "b"), "normal")
    urgent = propose((2, "b"), "urgent", 10)
    assert propose((2, "b"), "urgent", 10) == urgent
    selected = state.claim_proposal(
        "s", (2, "b"), {"normal": "v1", "urgent": "v1"}, now
    )
    assert selected["proposal_id"] == urgent
    state.settle_proposal(urgent, "delivering", now, {"draft": "a grounded update"})
    second = state.claim_proposal("s", (2, "b"), {"normal": "v1"}, now)
    assert second["proposal_id"] == normal
    state.close()
    state = DriftStateStore(path)
    statuses = {r["proposal_id"]: r["status"] for r in state.recent_proposals()}
    assert statuses == {
        stale: "superseded",
        normal: "interrupted",
        urgent: "needs_reconciliation",
    }
    assert (
        state.claim_proposal("s", (2, "b"), {"normal": "v1", "urgent": "v1"}, now)
        is None
    )
    with pytest.raises(ValueError):
        state.settle_proposal(urgent, "running", now)
    state.close()
