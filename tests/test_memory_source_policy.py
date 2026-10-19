"""Scheduler originals remain queryable without becoming learned user facts."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bus.events_lifecycle import TurnCommitted
from core.memory.markdown import (
    ConsolidateRequest,
    MarkdownMemoryMaintenance,
    MarkdownMemoryStore,
    _build_consolidation_source_ref,
    _format_conversation_for_consolidation,
    _select_consolidation_window,
)
from plugins.ledger.manager import SessionManager
from plugins.ledger.turn_source import is_memory_source, load_committed_turn_messages
from proactive_v2.loop import ProactiveLoop


@pytest.mark.parametrize(
    "metadata,eligible",
    [
        ({}, True),
        ({"source": "conversation"}, True),
        ({"source": "programmatic"}, True),
        ({"source": "legacy-unattributed"}, True),
        ({"source": "scheduler:fire"}, False),
        ({"source": "wake"}, False),
        ({"source": "drift"}, False),
        ({"source": "unrecognized"}, False),
        ({"skip_post_memory": True}, False),
        ({"proactive": True}, False),
    ],
)
def test_source_policy(metadata, eligible):
    assert is_memory_source(metadata) is eligible


def test_original_source_policy_applies_to_both_messages_and_preserves_human_correction(
    tmp_path,
):
    sessions = SessionManager(tmp_path)
    try:
        session = sessions.get_or_create("web:origin")
        for source in ("scheduler:task", "conversation"):
            user = session.add_message("user", "Current code is " + source)
            assistant = session.add_message("assistant", "Recorded", source=source)
            sessions.save(session)
            event = TurnCommitted(
                session_key=session.key,
                channel="web",
                chat_id="origin",
                user_message_id=user["id"],
                assistant_message_id=assistant["id"],
            )
            resolved = load_committed_turn_messages(tmp_path / "sessions.db", event)
            assert len(resolved) == (2 if source == "conversation" else 0)
        window = _select_consolidation_window(
            session, keep_count=0, consolidation_min_new_messages=1, archive_all=True
        )
        formatted = _format_conversation_for_consolidation(window.old_messages)
        assert "Current code is conversation" in formatted
        assert "Recorded" in formatted
        # The scheduled assistant is excluded even if an old producer omitted
        # skip_post_memory. The raw messages themselves are still present.
        assert session.messages[1]["id"] not in _build_consolidation_source_ref(window)
        assert len(session.messages) == 4
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_scheduler_only_window_advances_without_model_or_profile_write(tmp_path):
    store = MarkdownMemoryStore(tmp_path)
    provider = SimpleNamespace(
        chat=AsyncMock(side_effect=AssertionError("must not call model"))
    )
    maintenance = MarkdownMemoryMaintenance(
        store=store, provider=provider, model="fixture", keep_count=0
    )
    session = SimpleNamespace(
        key="web:origin",
        last_consolidated=0,
        messages=[
            {
                "id": str(i),
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "synthetic task",
                "source": "scheduler:fire",
                "skip_post_memory": True,
            }
            for i in range(6)
        ],
    )
    result = await maintenance.consolidate(
        ConsolidateRequest(session=session, force=True)
    )
    assert session.last_consolidated == 6 and result.consolidated_count == 6
    assert result.trace["excluded_sources"] and not provider.chat.called
    assert not store.read_long_term().strip()


def test_many_scheduler_messages_do_not_hide_human_context_or_reset_presence():
    now = datetime.now(timezone.utc)
    human_at = now - timedelta(hours=2)
    messages = [
        {
            "role": "user",
            "content": "I need memory recovery updates",
            "timestamp": human_at.isoformat(),
        }
    ]
    messages.extend(
        {
            "role": "user",
            "content": "automatic job",
            "source": "scheduler:fire",
            "timestamp": now.isoformat(),
        }
        for _ in range(30)
    )
    loop = ProactiveLoop.__new__(ProactiveLoop)
    loop._cfg = SimpleNamespace(session_key="web:origin")
    loop._sessions = SimpleNamespace(
        get_or_create=lambda key: SimpleNamespace(messages=messages)
    )
    assert loop._presence(now) == (human_at, 1)
    assert "memory recovery updates" in loop._recent_conversation("web:origin")
    assert "automatic job" not in loop._recent_conversation("web:origin")
