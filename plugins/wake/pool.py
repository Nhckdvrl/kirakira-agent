from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from plugins.eventmail.contract import REQUESTED_REVIEW

WAKE_ADMISSION_FLOOR = 0.02
WAKE_POOL_THRESHOLD = 1.5
# A source the owner has rated carries its own bounded attention weight; the
# bounds live with the policy that derives it, and anything outside them is a
# bad payload rather than a stronger preference.
WAKE_MIN_ATTENTION = 0.5
WAKE_MAX_ATTENTION = 1.5

_FRESHNESS_HALF_LIFE_HOURS = 18.0
_MISSING_PUBLICATION_CONFIDENCE = 0.03
_INELIGIBLE_CONFIDENCE_MULTIPLIER = 0.01


@dataclass(frozen=True, slots=True)
class PoolResult:
    should_wake: bool
    new_mass: float
    pool_mass: float
    threshold: float
    below_floor: int
    driver_item_id: str
    requested_reviews: int = 0
    # Mass this pool would have had with every source weighted neutrally, so a
    # decision influenced by feedback can be compared against the plain one.
    unweighted_pool_mass: float = 0.0


def attention_weight(event: Mapping[str, Any]) -> float:
    """The owner-feedback weight carried on one event, or neutral.

    A missing, malformed or out-of-range value is treated as neutral: feedback
    tunes how loud a rated source is, and a broken payload is not feedback.
    """
    # Content items reach the pool with their payload already flattened into the
    # event, exactly as review_policy is read, so the weight sits at this level.
    value = event.get("attention_weight")
    if type(value) not in (int, float) or isinstance(value, bool):
        return 1.0
    number = float(value)
    if not math.isfinite(number) or not WAKE_MIN_ATTENTION <= number <= WAKE_MAX_ATTENTION:
        return 1.0
    return number


def measure_pool(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    new_item_ids: set[str] | None = None,
) -> PoolResult:
    """Measure one fixed-score pool after time decay and floor filtering."""

    ranked = rank_events(events, now=now)
    new_identities = new_item_ids or set()
    contributions: list[tuple[str, str, float]] = []
    unweighted: list[float] = []
    below_floor = 0
    for event in ranked:
        item_id = str(event.get("id") or "")
        identity = str(event.get("_wake_admission_identity") or item_id)
        features = event["_wake_rank_features"]
        mass = float(features["admission_mass"])
        # The floor applies to the measured mass, so feedback changes how much a
        # source contributes, never whether it was admissible in the first place.
        plain = mass if mass >= WAKE_ADMISSION_FLOOR else 0.0
        contribution = plain * (1.0 if requests_review(event) else attention_weight(event))
        below_floor += plain == 0.0
        contributions.append((item_id, identity, contribution))
        unweighted.append(plain)
    pool_mass = sum(value for _, _, value in contributions)
    new_mass = sum(
        value for _, identity, value in contributions if identity in new_identities
    )
    driver = max(contributions, key=lambda item: item[2])[0] if contributions else ""
    requested = [event for event in ranked if requests_review(event)
                 and str(event.get("_wake_admission_identity") or event.get("id") or "") in new_identities]
    return PoolResult(
        should_wake=bool(requested) or (bool(new_identities) and pool_mass > WAKE_POOL_THRESHOLD),
        new_mass=new_mass,
        pool_mass=pool_mass,
        threshold=WAKE_POOL_THRESHOLD,
        below_floor=below_floor,
        driver_item_id=str(requested[0].get("id", "")) if requested else driver,
        requested_reviews=len(requested),
        unweighted_pool_mass=sum(unweighted),
    )


def rank_events(events: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """Square and decay fixed scores, then diversify only the page order."""

    scored: list[dict[str, Any]] = []
    for event in events:
        raw_initial = event.get("_wake_initial_score")
        if raw_initial is None:
            raw_interest = event.get("_wake_interest_score")
            if raw_interest is None:
                raw_interest = event.get("preprocess_score")
            initial = build_initial_score(
                _as_float(raw_interest),
                has_published_at=bool(event.get("published_at")),
                wake_eligible=event.get("wake_eligible") is not False,
            )
        else:
            initial = min(7.0, max(0.0, _as_float(raw_initial)))
        semantic = min(
            0.999, max(0.0, _as_float(event.get("_wake_semantic_interest")))
        )
        reference = _parse_time(
            event.get("published_at") or event.get("first_seen_at") or now,
            now,
        )
        age_hours = max(0.0, (now - reference).total_seconds() / 3600)
        freshness = math.exp(
            -math.log(2.0) * age_hours / _FRESHNESS_HALF_LIFE_HOURS
        )
        copied = dict(event)
        admission_mass = initial**2 * freshness
        copied["_wake_rank_score"] = admission_mass
        copied["_wake_rank_features"] = {
            "initial_score": initial,
            "semantic_interest": semantic,
            "freshness": freshness,
            "age_hours": age_hours,
            "admission_mass": admission_mass,
            "source_diversity": 1.0,
        }
        scored.append(copied)
    scored.sort(key=_rank_key, reverse=True)
    source_counts: dict[str, int] = {}
    for event in scored:
        source = str(event.get("source_id") or event.get("source") or "unknown")
        position = source_counts.get(source, 0)
        multiplier = 0.5**position
        source_counts[source] = position + 1
        event["_wake_rank_score"] = float(event["_wake_rank_score"]) * multiplier
        event["_wake_rank_features"]["source_diversity"] = multiplier
    return sorted(scored, key=_rank_key, reverse=True)


def build_initial_score(
    interest: float,
    *,
    has_published_at: bool,
    wake_eligible: bool,
) -> float:
    """Convert one 0..1 interest probability to the legacy admission-mass scale."""

    probability = min(0.999, max(0.0, interest))
    confidence = 1.0 if has_published_at else _MISSING_PUBLICATION_CONFIDENCE
    if not wake_eligible:
        confidence *= _INELIGIBLE_CONFIDENCE_MULTIPLIER
    return -math.log1p(-probability) * confidence


def requests_review(event: Mapping[str, Any]) -> bool:
    return (event.get("review_policy") == REQUESTED_REVIEW
            and event.get("wake_eligible") is not False
            and event.get("_wake_review_consumed") is not True)


def _rank_key(event: dict[str, Any]) -> tuple[bool, float, str]:
    return (
        requests_review(event),
        float(event["_wake_rank_score"]),
        str(event.get("published_at") or event.get("first_seen_at") or ""),
    )


def _parse_time(value: object, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback
    return (
        parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=fallback.tzinfo)
    )


def _as_float(value: object) -> float:
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0
