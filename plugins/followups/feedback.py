"""Turn recorded delivery feedback into a bounded attention weight.

Feedback on a delivered follow-up ("useful" / "irrelevant") used to be recorded
for inspection only. Here it becomes one explicit, bounded signal: how much a
watch's later items count toward the interest pool that decides whether to
contact the owner at all.

Three things are deliberately kept apart. Rating an item is the owner's
judgement of one delivery. The weight derived here is a prior for the same
watch's later items, never a verdict on any individual one. And whether the
owner is contacted stays with the pool and its floor, which this only feeds.

Properties this relies on, all covered by tests:
  * A watch with no ratings, or too few to mean anything, weighs exactly 1.0,
    so an unrated deployment behaves exactly as before.
  * The weight is clamped to [MIN_WEIGHT, MAX_WEIGHT]; no amount of feedback can
    silence a watch completely or let one dominate the pool.
  * It is monotone: another "irrelevant" never raises the weight, and another
    "useful" never lowers it.
  * It is a pure function of the counts, so the same records always yield the
    same weight and an offline replay can reproduce any decision.

An explicitly requested review bypasses the pool entirely, so no weight applies
to it. Suppressing something the owner asked to see is not what this is for.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

Value = Literal["useful", "irrelevant"]

# A watch needs this many ratings before any adjustment; below it one annoyed
# click would retune a source, which is noise rather than a preference.
MIN_RATINGS = 4
MIN_WEIGHT = 0.5
MAX_WEIGHT = 1.5
NEUTRAL_WEIGHT = 1.0


@dataclass(frozen=True, slots=True)
class Attention:
    """What the recorded ratings for one watch add up to."""

    useful: int
    irrelevant: int
    weight: float
    adjusted: bool
    reason: str

    @property
    def rated(self) -> int:
        return self.useful + self.irrelevant

    def as_dict(self) -> dict[str, object]:
        return {"useful": self.useful, "irrelevant": self.irrelevant,
                "rated": self.rated, "weight": self.weight,
                "adjusted": self.adjusted, "reason": self.reason}


def _clamp(value: float) -> float:
    return max(MIN_WEIGHT, min(MAX_WEIGHT, value))


def measure(records: Iterable[Mapping[str, object]]) -> Attention:
    """Aggregate one watch's stored feedback records into an attention weight.

    Records are the stored rows as written by the service; anything without a
    recognised value is ignored rather than guessed at, because an unreadable
    row is not evidence of either judgement.
    """
    useful = irrelevant = 0
    for record in records:
        match record.get("value"):
            case "useful":
                useful += 1
            case "irrelevant":
                irrelevant += 1
    rated = useful + irrelevant
    if rated < MIN_RATINGS:
        return Attention(useful, irrelevant, NEUTRAL_WEIGHT, False,
                         f"评价数 {rated} 少于 {MIN_RATINGS} 条，沿用默认权重")
    # Share of useful ratings, centred on neutral: all-useful reaches MAX,
    # all-irrelevant reaches MIN, and an even split stays at 1.0.
    share = useful / rated
    if share >= 0.5:
        weight = NEUTRAL_WEIGHT + (MAX_WEIGHT - NEUTRAL_WEIGHT) * (share - 0.5) * 2.0
    else:
        weight = NEUTRAL_WEIGHT - (NEUTRAL_WEIGHT - MIN_WEIGHT) * (0.5 - share) * 2.0
    weight = round(_clamp(weight), 4)
    return Attention(useful, irrelevant, weight, weight != NEUTRAL_WEIGHT,
                     f"{rated} 条评价中 {useful} 条有用，权重 {weight}")


def weight_of(records: Iterable[Mapping[str, object]]) -> float:
    """The weight alone, for callers that do not display the reasoning."""
    return measure(records).weight
