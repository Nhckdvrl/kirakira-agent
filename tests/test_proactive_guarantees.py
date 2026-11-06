"""Bounds on proactive attention, which is what makes the feedback loop safe.

A proactive agent that enough negative ratings can silence is useless, and one
that enough positive ratings can turn into a firehose is worse. Both are the
same bug -- an unbounded weight -- so the bounds are asserted over the whole
rating space rather than at a few points, and separately against payloads a
feed item could carry.
"""
from __future__ import annotations

import math

import pytest

from eval.proactive_guarantees import naive, ours, sweep
from plugins.followups.feedback import MAX_WEIGHT, MIN_RATINGS, MIN_WEIGHT


def test_no_amount_of_feedback_leaves_the_bounds() -> None:
    report = sweep(limit=25)
    silence = report["cannot_be_trained_into_silence"]
    reach = report["can_promote_as_well_as_suppress"]

    assert silence["ours_states_reaching_zero"] == 0
    assert report["monotone_in_the_useful_share"]["violations"] == 0
    assert report["waits_for_evidence"]["ours_states_moving_before_minimum_ratings"] == 0
    assert reach["ours_range"] == [MIN_WEIGHT, MAX_WEIGHT]

    # The two findings that do not depend on where we put our own bounds: the
    # obvious policy reaches zero, which silences a source for good, and it tops
    # out at neutral, so praise can only ever restore a source to the default
    # while criticism can bury it. Measuring it against our interval instead
    # would be partly definitional, so that count is secondary.
    assert silence["naive_states_reaching_zero"] > 0
    assert reach["naive_can_exceed_neutral"] is False
    assert reach["naive_range"][1] <= reach["neutral"]
    assert report["states_outside_our_bounds"]["ours"] == 0


def test_the_extremes_reach_the_bounds_without_passing_them() -> None:
    """Bounded must not mean inert: the ends have to be reachable."""
    assert ours(MIN_RATINGS, 0) == MAX_WEIGHT
    assert ours(0, MIN_RATINGS) == MIN_WEIGHT
    assert ours(500, 0) == MAX_WEIGHT
    assert ours(0, 500) == MIN_WEIGHT
    # And the unbounded policy is where the floor matters most.
    assert naive(0, 500) == 0.0


def test_a_crafted_payload_cannot_make_an_item_louder() -> None:
    """Feed items are not trusted input; the weight rides on the payload."""
    report = sweep(limit=4)
    hostile = report["hostile_payloads_cannot_raise_attention"]
    assert len(hostile) >= 8
    assert all(row["neutralised"] for row in hostile), hostile
    assert all(row["ours_resolved_weight"] == 1.0 for row in hostile)


@pytest.mark.parametrize("value", [10 ** 9, MAX_WEIGHT + 1e-9, -1.0, math.inf, math.nan])
def test_out_of_range_weights_are_neutral_rather_than_clamped(value: float) -> None:
    """Clamping a hostile value would still honour its direction; neutral does not."""
    from plugins.wake.pool import attention_weight

    assert attention_weight({"attention_weight": value}) == 1.0
    assert MIN_WEIGHT <= attention_weight({"attention_weight": value}) <= MAX_WEIGHT
