"""Fixed, label-independent selection and chronological LongMemEval inputs."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "knowledge-update",
    "temporal-reasoning",
)


def select(instances, per_type=6, seed="kirakira-20261019"):
    selected = []
    for kind in TYPES:
        rows = sorted(
            (x for x in instances if x.question_type == kind),
            key=lambda x: hashlib.sha256(
                (seed + ":" + x.question_id).encode()
            ).hexdigest(),
        )
        # Keep natural abstention frequency: selection never uses answers or answer IDs.
        selected.extend(rows[:per_type])
    return selected


def select_fixed(instances, per_type=6, selection=None):
    if selection is None:
        return select(instances, per_type)
    identities = json.loads(Path(selection).read_text())["case_ids"]
    if (
        not isinstance(identities, list)
        or not identities
        or any(not isinstance(identity, str) or not identity for identity in identities)
        or len(set(identities)) != len(identities)
    ):
        raise ValueError("selection requires unique nonempty case IDs")
    indexed = {instance.question_id: instance for instance in instances}
    return [indexed[identity] for identity in identities]


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        result = datetime.strptime(value, "%Y/%m/%d (%a) %H:%M")
    # Dataset dates contain no zone. A fixed UTC interpretation preserves order.
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def chronological(instance):
    if not (
        len(instance.haystack_sessions)
        == len(instance.haystack_dates)
        == len(instance.haystack_session_ids)
    ):
        raise ValueError("unaligned session IDs, dates, and messages")
    return sorted(
        zip(
            instance.haystack_dates,
            instance.haystack_session_ids,
            instance.haystack_sessions,
            strict=True,
        ),
        key=lambda row: timestamp(row[0]),
    )
