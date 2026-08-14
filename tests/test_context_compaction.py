"""Reference context-compaction contracts adapted to Kirakira's provider port."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.model_runtime.context_compaction import (
    SUMMARY_HEADINGS,
    CommittedContextUnit,
    ContextCompactionError,
    ContextCompactor,
    ContextPayloadSegments,
)
from session.compaction_runtime import SessionCompactionRuntime
from session.manager import SessionManager


_SUMMARY = "\n".join([heading + "\nvalue" for heading in SUMMARY_HEADINGS])


class _Provider:
    runtime_id = "test"
    model = "test-model"
    max_output_tokens = 100

    def __init__(self, context_window: int = 100_000) -> None:
        self.context_window = context_window
        self.calls: list[dict] = []

    def estimate_context_tokens(self, messages, tools):
        return sum(int(message.get("tokens", 1)) for message in messages) + len(tools)

    def estimate_appended_message_tokens(self, messages):
        return sum(int(message.get("tokens", 1)) for message in messages)

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=_SUMMARY, tool_calls=[], usage=None)


def _unit(seq: int, tokens: int) -> CommittedContextUnit:
    return CommittedContextUnit(
        source_from_seq=seq,
        consolidated_through_seq=seq,
        source_message_ids=(f"m{seq}",),
        messages=({"role": "user", "content": f"u{seq}", "tokens": tokens},),
        message_refs=((f"m{seq}", seq),),
    )


def test_reference_keep_recent_cut_is_whole_units() -> None:
    units = (_unit(1, 10_000), _unit(2, 15_000), _unit(3, 5_000))
    segments = ContextPayloadSegments(
        prefix=(),
        committed_units=units,
        current_anchor=({"role": "user", "content": "current", "tokens": 1},),
    )
    compactor = ContextCompactor(
        provider=_Provider(),
        model="test-model",
        scope_id="scope",
        payload_segments=segments,
        max_output_tokens=100,
        next_generation=1,
        keep_recent_tokens=20_000,
    )
    result = asyncio.run(
        compactor.prepare(segments.flatten(), pending_start=4, tools=[], force=True)
    )
    assert result.compacted
    assert result.checkpoint is not None
    assert [item["id"] for item in result.checkpoint.retained_tail] == ["m2", "m3"]


def test_reference_refuses_to_split_below_keep_recent_target() -> None:
    units = (_unit(1, 5_000), _unit(2, 5_000))
    compactor = ContextCompactor(
        provider=_Provider(),
        model="test-model",
        scope_id="scope",
        payload_segments=ContextPayloadSegments((), units, ()),
        max_output_tokens=100,
        next_generation=1,
        keep_recent_tokens=20_000,
    )
    with pytest.raises(ContextCompactionError, match="no_valid_cut"):
        compactor._select_units(list(units))


def test_session_ledger_is_append_only_and_projection_reloads(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:test")
    for index in range(3):
        session.add_message("user", f"u{index}")
        session.add_message("assistant", f"a{index}")
    manager.save(session)
    runtime = SessionCompactionRuntime(manager)
    projection = runtime.projection(session, prefix=[], current_anchor=[], pending=[])
    units = projection.segments.committed_units
    provider = _Provider(context_window=100)
    # Fixture-only token weights make two old units compactable while retaining one.
    weighted = tuple(
        CommittedContextUnit(
            unit.source_from_seq,
            unit.consolidated_through_seq,
            unit.source_message_ids,
            tuple({**message, "tokens": 30} for message in unit.messages),
            unit.message_refs,
        )
        for unit in units
    )
    segments = ContextPayloadSegments((), weighted, ())
    compactor = ContextCompactor(
        provider=provider,
        model="test-model",
        scope_id="cli:test-scope",
        payload_segments=segments,
        max_output_tokens=10,
        ledger_parent_generation=0,
        next_generation=1,
        keep_recent_tokens=50,
    )
    result = asyncio.run(
        compactor.prepare(segments.flatten(), pending_start=6, tools=[], force=True)
    )
    assert result.checkpoint is not None and result.checkpoint.committable
    runtime.commit_checkpoint(session, result.checkpoint, head=projection.head)
    assert len(session.messages) == 6
    reloaded = runtime.projection(session, prefix=[], current_anchor=[], pending=[])
    assert reloaded.active is not None
    assert reloaded.active.generation == 1
    assert "<session-context-compaction>" in reloaded.segments.prefix[0]["content"]
    manager.close()
