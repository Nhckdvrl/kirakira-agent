"""What bounds the proactive side, measured against an unbounded policy.

Sending on a schedule has no moat: cron plus one model call does it. The hard
part is the other three questions -- should this go out, on what evidence, and
what happens when it was wrong -- and two of those come down to whether
feedback can be pushed to an extreme.

A proactive agent that can be trained into silence is useless, and one that can
be trained into noise is worse. Both failures look the same from the inside:
the weight that decides whether an item clears the wake threshold just keeps
moving. So this measures the policy against the obvious one -- take the share
of ratings that said useful and use it directly -- over the whole input space
rather than a sampled bit of it.

A third axis is adversarial rather than statistical. The weight rides on the
event payload, and feed items are not trusted input, so a crafted value must
not be able to make an item louder than the owner's own ratings allow.

No chat model, no API key, no workspace: these are pure functions swept
exhaustively.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from plugins.followups.feedback import MAX_WEIGHT, MIN_RATINGS, MIN_WEIGHT, measure
from plugins.wake.pool import WAKE_POOL_THRESHOLD, attention_weight


def ours(useful: int, irrelevant: int) -> float:
    records = [{"value": "useful"}] * useful + [{"value": "irrelevant"}] * irrelevant
    return measure(records).weight


def naive(useful: int, irrelevant: int) -> float:
    """Use the share of useful ratings directly, which is the obvious policy.

    It needs no tuning and reads as fair, which is exactly why it is the one to
    beat: its range reaches 0, and 0 means the source can never clear the wake
    threshold again no matter what it later publishes.
    """
    rated = useful + irrelevant
    return 1.0 if rated == 0 else useful / rated


# Payloads a feed item could carry. The weight is read off the event, so these
# are reachable by anything that can get an item into the pool.
HOSTILE = (
    ("far above the ceiling", 10 ** 6),
    ("just above the ceiling", MAX_WEIGHT + 0.001),
    ("negative", -5.0),
    ("infinite", math.inf),
    ("not a number", math.nan),
    ("boolean true", True),
    ("string", "1.5"),
    ("missing", None),
)


def sweep(limit: int) -> dict[str, object]:
    silenced_ours = silenced_naive = 0
    out_of_bounds_ours = out_of_bounds_naive = 0
    moved_before_evidence_ours = moved_before_evidence_naive = 0
    states = 0
    monotone_violations = 0
    previous_by_rated: dict[int, list[tuple[float, float]]] = {}

    for useful in range(limit + 1):
        for irrelevant in range(limit + 1):
            rated = useful + irrelevant
            if rated == 0:
                continue
            states += 1
            mine, theirs = ours(useful, irrelevant), naive(useful, irrelevant)
            # Silenced: a weight at or below zero can never clear the threshold.
            silenced_ours += mine <= 0.0
            silenced_naive += theirs <= 0.0
            out_of_bounds_ours += not (MIN_WEIGHT <= mine <= MAX_WEIGHT)
            out_of_bounds_naive += not (MIN_WEIGHT <= theirs <= MAX_WEIGHT)
            # Moving on one or two ratings is noise, not evidence.
            if rated < MIN_RATINGS:
                moved_before_evidence_ours += mine != 1.0
                moved_before_evidence_naive += theirs != 1.0
            previous_by_rated.setdefault(rated, []).append((useful / rated, mine))

    # More useful ratings must never lower the weight at a fixed denominator.
    for rows in previous_by_rated.values():
        rows.sort()
        for (_, earlier), (_, later) in zip(rows, rows[1:], strict=False):
            if later < earlier - 1e-9:
                monotone_violations += 1

    hostile = []
    for label, value in HOSTILE:
        event = {} if value is None else {"attention_weight": value}
        resolved = attention_weight(event)
        hostile.append({"payload": label, "ours_resolved_weight": resolved,
                        "neutralised": resolved == 1.0,
                        "naive_would_use": "1.0 (neutral)" if value is None else repr(value)})

    return {
        "kind": "bounds on proactive attention, swept exhaustively against an unbounded policy",
        "conditions": {"ratings_per_side_up_to": limit, "rating_states": states,
                       "chat_model": None, "api_key_required": False,
                       "bounds": [MIN_WEIGHT, MAX_WEIGHT],
                       "wake_threshold": WAKE_POOL_THRESHOLD,
                       "ratings_required_before_adjusting": MIN_RATINGS,
                       "baseline": "weight = useful / rated, used directly"},
        "cannot_be_trained_into_silence": {
            "ours_states_reaching_zero": silenced_ours,
            "naive_states_reaching_zero": silenced_naive,
            "note": ("权重为 0 的来源再也无法越过唤醒阈值，等于被永久消音。"
                     "本项目的下界把它挡住：再多负面反馈也只能压到 "
                     f"{MIN_WEIGHT}，不能归零。"),
        },
        "can_promote_as_well_as_suppress": {
            # The substantive failure, not a definitional one: the obvious policy
            # tops out at exactly the neutral weight, so feedback can only ever
            # reduce a source's reach. Over time that is a one-way ratchet toward
            # silence, whatever the owner says they find useful.
            "ours_range": [min(ours(u, i) for u in range(limit + 1)
                               for i in range(limit + 1) if u + i),
                           max(ours(u, i) for u in range(limit + 1)
                               for i in range(limit + 1) if u + i)],
            "naive_range": [min(naive(u, i) for u in range(limit + 1)
                                for i in range(limit + 1) if u + i),
                            max(naive(u, i) for u in range(limit + 1)
                                for i in range(limit + 1) if u + i)],
            "neutral": 1.0,
            "naive_can_exceed_neutral": max(
                naive(u, i) for u in range(limit + 1)
                for i in range(limit + 1) if u + i) > 1.0,
            "note": ("朴素策略的上限恰好等于中性值，所以它只能压低、不能抬高——"
                     "好评最多把来源还原到默认，差评却能一路压到零。"),
        },
        "states_outside_our_bounds": {
            # Kept, but secondary and labelled: measuring another policy against
            # this policy's own interval is partly definitional, so it is not the
            # headline. The two findings above do not depend on our bounds.
            "ours": out_of_bounds_ours,
            "naive": out_of_bounds_naive,
            "caveat": "以本项目的区间去量另一种策略，这个计数有定义性成分，不作为主结论。",
        },
        "waits_for_evidence": {
            "ours_states_moving_before_minimum_ratings": moved_before_evidence_ours,
            "naive_states_moving_before_minimum_ratings": moved_before_evidence_naive,
            "minimum": MIN_RATINGS,
        },
        "monotone_in_the_useful_share": {"violations": monotone_violations},
        "hostile_payloads_cannot_raise_attention": hostile,
        "claim_limits": ("这一组量的是边界与单调性，不是判断质量。"
                         "主动联系该不该发、依据对不对，由受控邮件实验单独衡量，"
                         "那边的分母仍然很小。"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = sweep(args.limit)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
