"""Pooling two runs is where a growing sample quietly becomes a cherry-pick.

The module exists to make the two easy mistakes impossible rather than
unlikely: counting a question twice because the runs overlapped, and comparing
arms on questions only one of them answered.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.pool_reports import compare, pool

OURS, DENSE = "akasha-fused-excerpts", "dense-excerpts"


def write(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
    return path


def trial(question: str, system: str, correct: bool, category: str = "multi-session") -> dict:
    return {"question_id": question, "system": system, "correct": correct,
            "category": category}


def test_overlapping_runs_are_refused_rather_than_double_counted(tmp_path: Path) -> None:
    shared = [trial("q1", OURS, True), trial("q1", DENSE, False)]
    first = write(tmp_path / "a.json", shared)
    second = write(tmp_path / "b.json", shared)
    with pytest.raises(ValueError, match="shares 1 questions"):
        pool([first, second], OURS, DENSE)


def test_disjoint_runs_pool_and_keep_every_question(tmp_path: Path) -> None:
    first = write(tmp_path / "a.json",
                  [trial("q1", OURS, True), trial("q1", DENSE, False)])
    second = write(tmp_path / "b.json",
                   [trial("q2", OURS, False), trial("q2", DENSE, True)])
    result = pool([first, second], OURS, DENSE)
    assert [run["questions"] for run in result["runs"]] == [1, 1]
    assert result["all_questions"]["paired_questions"] == 2
    # One win each: the totals tie and the test cannot separate them.
    assert result["all_questions"]["only_first"] == 1
    assert result["all_questions"]["only_second"] == 1
    assert result["all_questions"]["p_value"] == 1.0


def test_a_question_only_one_arm_reached_is_dropped_and_counted(tmp_path: Path) -> None:
    rows = [trial("q1", OURS, True), trial("q1", DENSE, True),
            trial("q2", OURS, True)]  # the baseline never answered q2
    result = pool([write(tmp_path / "a.json", rows)], OURS, DENSE)
    assert result["questions_answered_by_only_one_run"] == 1
    assert result["all_questions"]["paired_questions"] == 1


def test_abstention_is_excluded_from_the_headline(tmp_path: Path) -> None:
    """A system given no history abstains correctly, which flatters the total."""
    rows = [trial("q1", OURS, True, "multi-session"),
            trial("q1", DENSE, False, "multi-session"),
            trial("a1", OURS, True, "abstention"),
            trial("a1", DENSE, True, "abstention")]
    result = pool([write(tmp_path / "a.json", rows)], OURS, DENSE)
    assert result["all_questions"]["paired_questions"] == 2
    assert result["answerable_only"]["paired_questions"] == 1


def test_the_difference_interval_widens_as_disagreements_grow() -> None:
    """Few discordant pairs must not look like a precise estimate."""
    tight = compare({f"q{n}": True for n in range(100)},
                    {f"q{n}": True for n in range(100)})
    loose = compare({f"q{n}": n % 2 == 0 for n in range(100)},
                    {f"q{n}": n % 3 == 0 for n in range(100)})
    tight_width = tight["difference_95_points"][1] - tight["difference_95_points"][0]
    loose_width = loose["difference_95_points"][1] - loose["difference_95_points"][0]
    assert tight_width == 0.0 and loose_width > tight_width
