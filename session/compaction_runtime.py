"""Session projection and ledger owner aligned with the upstream design.

This deliberately has no import from ``Reference/``.  The only Kirakira-specific
adaptation is that the existing Markdown/Akasha cursor remains independent from
the context-compaction cursor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.model_runtime.context_compaction import (
    ActiveCompaction,
    CommittedContextUnit,
    ContextCompaction,
    ContextPayloadSegments,
    build_compaction_messages,
)
from session.manager import CompactionHead, PersistedSessionCompaction, Session, SessionManager


@dataclass(frozen=True)
class CompactionProjection:
    segments: ContextPayloadSegments
    active: ActiveCompaction | None
    head: CompactionHead


class SessionCompactionRuntime:
    """Own the exact SessionDB-backed prompt projection and checkpoint commit."""

    def __init__(self, session_manager: SessionManager) -> None:
        self._session_manager = session_manager

    def projection(
        self,
        session: Session,
        *,
        prefix: list[dict[str, Any]],
        current_anchor: list[dict[str, Any]],
        pending: list[dict[str, Any]],
    ) -> CompactionProjection:
        head = self._session_manager.get_compaction_head(session.key)
        active_row = self._session_manager.get_active_compaction(session.key)
        active: ActiveCompaction | None = None
        projected_prefix = list(prefix)
        if active_row is None:
            units = list(session.history_units(after_seq=-1))
        else:
            active = _active(active_row)
            projected_prefix.extend(
                build_compaction_messages(
                    active.summary,
                    generation=active.generation,
                    source_ref=active_row.source_ref,
                )
            )
            tail_units = _retained_tail_units(active_row.retained_tail)
            if tail_units:
                after_seq = max(unit.consolidated_through_seq for unit in tail_units)
            else:
                after_seq = active.consolidated_through_seq
            units = [*tail_units, *session.history_units(after_seq=after_seq)]
        return CompactionProjection(
            segments=ContextPayloadSegments(
                prefix=tuple(projected_prefix),
                committed_units=tuple(units),
                current_anchor=tuple(current_anchor),
                pending=tuple(pending),
            ),
            active=active,
            head=head,
        )

    def commit_checkpoint(
        self,
        session: Session,
        checkpoint: ContextCompaction,
        *,
        head: CompactionHead,
    ) -> PersistedSessionCompaction:
        return self._session_manager.persist_context_compaction(
            session, checkpoint, head=head
        )


def _active(row: PersistedSessionCompaction) -> ActiveCompaction:
    return ActiveCompaction(
        generation=row.generation,
        summary=row.summary,
        source_from_seq=row.source_from_seq,
        consolidated_through_seq=row.consolidated_through_seq,
        source_message_ids=row.source_message_ids,
        retained_tail=row.retained_tail,
    )


def _retained_tail_units(
    retained_tail: tuple[dict[str, Any], ...],
) -> list[CommittedContextUnit]:
    """Reconstruct the retained tail with the upstream compaction contract."""

    grouped: dict[str, tuple[list[dict[str, Any]], list[tuple[str, int]]]] = {}
    for item in retained_tail:
        message = item.get("message")
        raw_id = item.get("id")
        raw_seq = item.get("seq")
        unit_ref = item.get("unit_ref")
        if (
            not isinstance(message, dict)
            or not isinstance(raw_id, str)
            or not raw_id
            or not isinstance(raw_seq, int)
            or isinstance(raw_seq, bool)
            or raw_seq < 0
            or not isinstance(unit_ref, str)
            or not unit_ref.strip()
        ):
            raise ValueError("compaction retained_tail provenance 无效")
        messages, refs = grouped.setdefault(unit_ref, ([], []))
        messages.append(dict(message))
        refs.append((raw_id, raw_seq))
    return [
        CommittedContextUnit(
            source_from_seq=min(seq for _, seq in refs),
            consolidated_through_seq=max(seq for _, seq in refs),
            source_message_ids=tuple(dict.fromkeys(message_id for message_id, _ in refs)),
            messages=tuple(messages),
            message_refs=tuple(refs),
        )
        for messages, refs in grouped.values()
    ]
