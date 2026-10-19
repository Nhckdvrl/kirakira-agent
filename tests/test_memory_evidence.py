"""Original-message evidence stays complete and coherent across recall and fetch."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core.memory.engine import MemoryQuery
from plugins.akasha2.infrastructure.persistence import logical_state_sha256
from plugins.ledger.manager import SessionManager
from tests.test_akasha2_runtime import add, engine


@pytest.mark.asyncio
async def test_recall_range_fetches_exact_originals_and_merges_context(tmp_path):
    sessions = SessionManager(tmp_path)
    memory = engine(tmp_path)
    try:
        for index in range(4):
            add(sessions, f"fixture:{index % 2}", f"original episode {index}")
        await memory.catch_up()
        result = await memory.query(
            MemoryQuery(
                "original episode",
                limit=4,
                timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
        )
        assert len(result.records) == 4
        for record in result.records:
            evidence = record.evidence[0]
            rows = sessions.fetch_messages(evidence.source_ref, context=0)
            assert [row["source_ref"] for row in rows] == evidence.refs
            assert [row["role"] for row in rows] == ["user", "assistant"]
            assert all(row["is_anchor"] for row in rows)
            assert rows[0]["content"] in record.summary
        refs = [ref for record in result.records for ref in record.evidence[0].refs]
        rows = sessions.fetch_messages(json.dumps(refs + refs[:1]), context=1)
        assert len(rows) == len(refs) == len({row["source_ref"] for row in rows})
        assert len({row["session_key"] for row in rows}) == 2
        assert result.trace["source_snapshot"] is True
        await memory.aclose()
        before = logical_state_sha256(tmp_path / "memory/akasha2.db")
        sessions.fetch_messages(result.records[0].evidence[0].source_ref)
        assert logical_state_sha256(tmp_path / "memory/akasha2.db") == before
    finally:
        await memory.aclose()
        sessions.close()


@pytest.mark.parametrize("references", [[], [1], ["valid", "missing"]])
def test_invalid_or_partly_missing_range_never_returns_partial_evidence(
    tmp_path, references
):
    sessions = SessionManager(tmp_path)
    try:
        session = sessions.get_or_create("fixture")
        session.add_message("user", "present", id="valid")
        sessions.save(session)
        with pytest.raises(ValueError):
            sessions.fetch_messages(json.dumps(references))
        assert sessions.fetch_messages("fixture:99") == []
        assert sessions.fetch_messages("fixture:0")[0]["content"] == "present"
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_query_verifies_all_hits_against_one_source_revision(
    tmp_path, monkeypatch
):
    sessions = SessionManager(tmp_path)
    memory = engine(tmp_path)
    try:
        for index in range(4):
            add(sessions, f"fixture:{index}", f"original revision {index}")
        await memory.catch_up()
        original = memory._messages
        changed = False

        def concurrent_source_change(assistant_id, source=None):
            nonlocal changed
            result = original(assistant_id, source)
            if not changed:
                changed = True
                victim = next(
                    t
                    for t in memory._cycle.turns
                    if t.assistant_message_id != assistant_id
                )
                with sqlite3.connect(tmp_path / "sessions.db") as writer:
                    writer.execute(
                        "UPDATE messages SET content='changed revision' WHERE id=?",
                        (victim.user_message_id,),
                    )
            return result

        monkeypatch.setattr(memory, "_messages", concurrent_source_change)
        query = MemoryQuery(
            "original revision",
            limit=4,
            timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        result = await memory.query(query)
        assert len(result.records) == 4
        assert all(
            "changed revision" not in record.summary for record in result.records
        )
        with pytest.raises(ValueError, match="original messages changed"):
            await memory.query(query)
    finally:
        await memory.aclose()
        sessions.close()
