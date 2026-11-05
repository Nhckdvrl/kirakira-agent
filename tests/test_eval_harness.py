"""Invariants of the evaluation harness itself.

A measurement harness can be wrong in ways the product's own tests never see.
Three defects reached published numbers before these existed: a reader output
budget clamped below what a reasoning reader needs to emit its one required
function call, a navigation fixture whose answer was not determinable from the
originals, and a summary that counted a truncated response as a refusal to
answer. Each one is asserted here.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from eval.memory_depth import ALL_SYSTEMS, followup_corpus, summary

# Published artifacts embed their own corpus, so verification does not depend on
# this generator. Reproducing a published report from source does, and a silent
# edit to a frozen fixture would make an old report unreproducible while every
# other test still passed.
PUBLISHED_FIXTURES = {
    1: "a3f21d747df2b444697188ad1f360c3d5e6cd85bd25cf7929780a1acbb5bab68",
    2: "fc0a8f0654e8b0b66f4452604d48552cb1772973bd7bb0b5fb75c1347c3a088f",
}


def fixture_sha256(fixture: dict) -> str:
    return hashlib.sha256(
        json.dumps(fixture, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("version", sorted(PUBLISHED_FIXTURES))
def test_published_navigation_fixtures_are_frozen(version: int) -> None:
    fixture, _ = followup_corpus(4, version=version)
    assert fixture_sha256(fixture) == PUBLISHED_FIXTURES[version], (
        f"source-followup-v{version} changed; published reports cite this corpus")


def test_fixture_versions_are_distinct() -> None:
    shas = {v: fixture_sha256(followup_corpus(4, version=v)[0]) for v in (1, 2, 3)}
    assert len(set(shas.values())) == 3, shas


def _originals(fixture: dict) -> list[str]:
    return [session[0]["content"] for session in fixture["haystack_sessions"]]


def test_navigation_answers_are_determinable_from_the_originals() -> None:
    """The reader must be able to reach one answer without guessing a mapping.

    Version 2 named the carrier field with a term the question never uses.
    long-tail and mid-document still connected the two through their directory
    note, one inference away, and readers demonstrably made it. head-control
    carried no note at all, so nothing in the originals tied the carrier to the
    asked field, while a separate message stated no such number existed yet:
    UNKNOWN was the better-supported answer and the family measured nothing.
    Version 3 states the mapping in every report, so no family depends on an
    inference the fixture never wrote down.
    """
    fixture, cases = followup_corpus(4, version=3)
    originals = _originals(fixture)
    for case in cases:
        if case["expected"] == "UNKNOWN":
            continue
        carriers = [text for text in originals if case["expected"] in text]
        assert len(carriers) == 1, (case["id"], len(carriers))
        report = carriers[0]
        # Whatever the carrier line is called, the report says once which field
        # registers the thing the question asks for.
        asked = case["query"].removeprefix(case["project"] + "的").removesuffix("是什么？")
        assert asked in report, (case["id"], asked)
        # The decoy is reachable too, and is marked invalid rather than merely absent.
        decoys = [text for text in originals if case["forbidden"] in text]
        assert len(decoys) == 1 and "不能用作正式结果" in decoys[0], case["id"]


def test_unknown_controls_have_no_answer_anywhere() -> None:
    fixture, cases = followup_corpus(4, version=3)
    originals = "\n".join(_originals(fixture))
    for case in cases:
        if case["expected"] != "UNKNOWN":
            continue
        assert "过敏" in case["query"]
        assert "没有采集任何工作人员的食物过敏资料" in originals


def test_navigation_values_are_not_leaked_to_the_reader() -> None:
    """No case may hand the reader the value or an exact offset."""
    _, cases = followup_corpus(4, version=3)
    for case in cases:
        assert case["expected"] not in case["query"], case["id"]
        assert case["forbidden"] not in case["query"], case["id"]
        assert not any(character.isdigit() for character in case["query"]), case["id"]


def test_reader_output_budget_is_not_clamped_below_the_configuration() -> None:
    """A truncated reader cannot emit its one required call.

    The harness used to clamp this to a constant, which a reasoning reader
    exhausts on its own thinking; the scorer then recorded a protocol failure
    the reader never committed.
    """
    source = (__import__("pathlib").Path("eval/memory_depth.py")).read_text()
    assert "max_output_tokens=min(1024, model.max_output_tokens)" not in source
    assert "reader_output_tokens = args.reader_output_tokens or model.max_output_tokens" in source


def test_summary_separates_truncation_from_refusal() -> None:
    system = ALL_SYSTEMS[0]

    def row(expected, finish_reason, tool_calls):
        return {"system": system, "expected": expected, "reads": [], "family": "head-control",
                "initial_material_chars": 100, "retained_evidence_chars": 100,
                "resent_evidence_chars": 100, "calls": [
                    {"finish_reason": finish_reason, "tool_calls": tool_calls, "usage": None}]}

    rows = [
        row("KF-1", "length", []),       # cut off mid-thought
        row("KF-2", "stop", []),         # answered in prose instead of calling
        row("KF-3", "tool_calls", [{"name": "answer"}]),
    ]
    out = summary(rows, (system,))[system]
    assert out["protocol_failures"] == 2
    assert out["truncated_calls"] == 1
    assert out["prose_calls"] == 1
