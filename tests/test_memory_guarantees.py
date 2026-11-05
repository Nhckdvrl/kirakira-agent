"""The design properties this project claims over a stock retrieval memory.

These are the claims an interviewer is entitled to push on, so they are pinned
as regressions rather than left to a report: a receipt re-renders identically
after the index grows, a read outside the receipt is refused while a legitimate
one is not, and every recall records what caused it.
"""
from __future__ import annotations

import pytest

from eval.memory_guarantees import StockMemory, cosine, measure, vector


def test_vectors_are_stable_and_content_dependent() -> None:
    assert vector("同一段文本") == vector("同一段文本")
    assert vector("甲") != vector("乙")
    assert cosine(vector("甲"), vector("甲")) == pytest.approx(1.0, abs=1e-9)


def test_the_baseline_is_a_fair_stand_in_not_a_straw_man() -> None:
    """It must actually retrieve, and it must be free of per-request state.

    A baseline that retrieved badly would make the comparison meaningless, and
    one that kept a receipt would not be the pattern being compared against.
    """
    stock = StockMemory(limit=2)
    for key, text in (("a", "项目预算 BUDGET=700"), ("b", "场地与时间安排"), ("c", "无关内容")):
        stock.remember(key, text)
    assert len(stock.search("项目预算 BUDGET=700")) == 2
    assert stock.search("项目预算 BUDGET=700")[0] == "a", "baseline must rank the match first"
    # No notion of "this turn was not entitled to that": anything stored is readable.
    assert stock.read_any("c") is not None
    assert not hasattr(stock, "receipts")


@pytest.mark.asyncio
async def test_receipt_design_holds_its_three_properties() -> None:
    report = await measure(seed_turns=4, live_turns=3)

    ours, _, total = report["reproducibility"]["ours_identical_after_index_growth"].partition("/")
    assert ours == total, "a saved receipt must re-render identically after the index grows"

    confinement = report["read_confinement"]
    assert confinement["illegal_attempts"] > 0
    assert confinement["ours_refused"] == confinement["illegal_attempts"]
    allowed, _, controls = confinement["ours_allowed_control"].partition("/")
    assert allowed == controls, "confinement must not break a legitimate re-read"
    # Each refusal path is distinct; collapsing them would hide which check fired.
    assert len(confinement["refusal_reasons"]) == 3

    attribution = report["attribution"]
    caused, _, receipts = attribution["ours_receipts_with_a_recorded_cause"].partition("/")
    assert caused == receipts and int(receipts) > 0

    fidelity = report["verbatim_fidelity"]
    assert fidelity["passages_checked"] > 0
    # Material is read from the stored body, never from a model-written summary,
    # so every passage must be a byte-exact slice of a real original. If this
    # ever fails, citing a character range stops meaning anything.
    assert fidelity["byte_exact_slices_of_a_real_original"] == fidelity["passages_checked"]

    assert report["repetition"]["ours_already_quoted_bodies_injected"] == 0
    # The comparison is only worth reporting if the baseline actually differs.
    assert report["repetition"]["stock_already_quoted_bodies_injected"] > 0

    # The limits of the claim travel with it.
    assert "新的召回查询" in report["claim_limits"] and "浮出新的原文" in report["claim_limits"]
    assert report["conditions"]["chat_model"] is None
