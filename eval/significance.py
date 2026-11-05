"""Paired comparison statistics, so a difference is reported with its weight.

A fraction on its own invites a directional reading that the sample may not
support. Two systems answering the same frozen questions are paired data, so
the question is not "which total is larger" but "do the questions they disagree
on lean one way further than chance would". That is McNemar's exact test, and
with the handful of discordant pairs these suites produce the exact binomial
form is the right one -- the chi-square approximation is not valid there.

Pure Python on purpose: this must run wherever the harness runs, and a
statistics dependency is not worth adding for two formulas.
"""

from __future__ import annotations

from math import comb, sqrt


def wilson_interval(correct: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """Score interval for a proportion.

    Preferred over the normal approximation because these denominators are
    small and near the boundaries, where the textbook interval runs past 0 or 1
    and understates the uncertainty that matters most here.
    """
    if total <= 0:
        return (0.0, 1.0)
    if not 0 <= correct <= total:
        raise ValueError("correct must lie within total")
    phat = correct / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    half = z * sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(only_first: int, only_second: int) -> float:
    """Two-sided exact p for paired wins, counting only the disagreements.

    Questions both systems answer the same way carry no information about which
    is better, so they are not in the denominator. That is why a large shared
    score can still leave a comparison undecided.
    """
    if only_first < 0 or only_second < 0:
        raise ValueError("counts must not be negative")
    discordant = only_first + only_second
    if discordant == 0:
        return 1.0
    tail = sum(comb(discordant, k) for k in range(min(only_first, only_second) + 1))
    return min(1.0, 2 * tail / 2 ** discordant)


def paired_comparison(first: dict[str, bool], second: dict[str, bool]) -> dict[str, object]:
    """Compare two systems over the questions both actually answered."""
    shared = sorted(set(first) & set(second))
    only_first = sum(1 for key in shared if first[key] and not second[key])
    only_second = sum(1 for key in shared if second[key] and not first[key])
    both = sum(1 for key in shared if first[key] and second[key])
    p = mcnemar_exact(only_first, only_second)
    return {
        "paired_questions": len(shared), "both_correct": both,
        "neither_correct": len(shared) - both - only_first - only_second,
        "only_first": only_first, "only_second": only_second,
        "discordant": only_first + only_second, "p_value": round(p, 4),
        # Stated rather than left to the reader: with these denominators the
        # usual outcome is that the suite cannot separate two retrieval methods,
        # and a report that prints only the totals hides that.
        "separates": bool(p < 0.05),
    }
