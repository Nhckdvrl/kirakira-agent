"""Persistent interest estimates feed a decaying pool, not a per-item send vote."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class InterestScore:
    interest: float
    confidence: float
    eligible: bool
    reason: str

    def __post_init__(self):
        for value in (self.interest, self.confidence):
            if (
                isinstance(value, bool)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError("interest and confidence must be finite probabilities")
        if not isinstance(self.eligible, bool) or not self.reason.strip():
            raise ValueError("eligibility and explanation are required")

    def initial(self, *, timestamp_present=True):
        return (
            -math.log1p(-min(self.interest, 1 - 1e-9))
            * self.confidence
            * (1 if timestamp_present else 0.03)
            * (1 if self.eligible else 0.01)
        )


def mass(initial, seen_at, now, *, half_life_hours=18):
    if half_life_hours <= 0:
        raise ValueError("half life must be positive")
    age = max(0.0, (now - seen_at).total_seconds())
    return initial * initial * math.exp(-math.log(2) * age / (half_life_hours * 3600))


def shortlist(items, *, limit=5):
    # Source diversity affects display rank, never the threshold's pool mass.
    remaining, selected, counts = list(items), [], {}
    while remaining and len(selected) < limit:
        item = max(
            remaining,
            key=lambda row: (
                row["_pool_mass"] * 0.5 ** counts.get(row.get("_source"), 0),
                row["item_id"],
            ),
        )
        remaining.remove(item)
        selected.append(item)
        source = item.get("_source")
        counts[source] = counts.get(source, 0) + 1
    return selected
