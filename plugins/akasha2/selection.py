"""Host readout: exact dense cues plus deduplicated associative completion.

Mirrors Akashic d85b00c plugins/akasha/recalls.py::select_hits. Kirakira's limit
is a total record budget, so completion fills the remaining slots after dense.
The pinned memory cycle and its learned state are never changed here.

Adapted under MIT, copyright (c) 2026 kachofugetsu09; see LICENSE in this package.
"""

from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass(frozen=True)
class SelectedHit:
    node_id: int
    score: float
    lane: str
    sources: tuple[str, ...]
    basin_ids: tuple[str, ...] = ()


def select_hits(
    turns,
    ticket,
    cue,
    *,
    inhibited,
    limit,
    strong=False,
    time_start=None,
    time_end=None,
):
    if ticket.state_version != len(turns):
        raise ValueError("readout ticket does not match the learned graph")
    if limit <= 0:
        return ()

    def matches(turn, sources):
        started = datetime.fromisoformat(turn.started_at)
        return (
            (time_start is None or started >= time_start)
            and (time_end is None or started <= time_end)
            and (not strong or any(source != "relative_tail" for source in sources))
        )

    dense = []
    if cue.user_dense is not None:
        for turn in turns:
            if turn.node_id in inhibited or not matches(turn, ("direct_dense",)):
                continue
            scores = [
                float(np.dot(cue.user_dense, vector))
                for vector in (turn.user_dense, turn.assistant_dense)
                if vector is not None
            ]
            if scores:
                dense.append((max(scores), turn.node_id))
    dense.sort(key=lambda item: (-item[0], item[1]))
    selected = [
        SelectedHit(node, score, "dense", ("direct_dense",))
        for score, node in dense[: min(5, limit)]
    ]
    direct = {item.node_id for item in selected}
    completion = []
    for item in ticket.completion.items:
        turn = turns[item.node_id]
        if (
            item.node_id in direct
            or item.node_id in inhibited
            or not matches(turn, item.sources)
        ):
            continue
        if len(selected) + len(completion) >= limit:
            break
        completion.append(
            SelectedHit(
                item.node_id, item.score, "completion", item.sources, item.basin_ids
            )
        )

    def newest(item):
        turn = turns[item.node_id]
        return datetime.fromisoformat(turn.started_at), turn.turn_id.encode()

    return tuple(
        sorted(selected, key=newest, reverse=True)
        + sorted(completion, key=newest, reverse=True)
    )
