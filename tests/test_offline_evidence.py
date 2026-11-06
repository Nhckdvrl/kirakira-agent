"""One command has to regenerate every claim that needs no key.

The argument lives on one page; the evidence for it should not be spread over
three entry points with three output shapes, because the first thing anyone
checking it will do is try to run it.
"""
from __future__ import annotations

from eval.offline_evidence import rows


def test_collect_wires_the_three_suites_without_running_them() -> None:
    """The aggregate adds no measurement of its own, so do not re-run them here.

    memory_guarantees, proactive_guarantees and cost_report each have their own
    regressions; re-measuring all three through this module costs about two
    minutes and proves nothing new. What is worth pinning is that this module
    still calls all three and still takes the knobs that keep the expensive one
    affordable on a busy machine.
    """
    import inspect

    from eval import offline_evidence

    source = inspect.getsource(offline_evidence)
    for suite in ("memory_guarantees.measure", "proactive_guarantees.sweep",
                  "cost_report.collect"):
        assert suite in source, suite
    parameters = inspect.signature(offline_evidence.collect).parameters
    for knob in ("runs", "limit", "seed_turns", "live_turns", "learn_turns", "queries"):
        assert knob in parameters, knob


def test_the_table_builder_needs_no_io() -> None:
    """Formatting is separable from measuring, so a shape change fails loudly."""
    guarantees = {
        "reproducibility": {"ours_identical_after_index_growth": "5/5",
                            "stock_identical_after_index_growth": "3/5",
                            "stock_cached_identical_after_index_growth": "5/5"},
        "read_confinement": {"ours_refused": 15, "illegal_attempts": 15,
                             "stock_refused": 0, "ours_allowed_control": "5/5"},
        "attribution": {"ours_receipts_with_a_recorded_cause": "5/5",
                        "stock_receipts_with_a_recorded_cause": "0/5"},
        "verbatim_fidelity": {"byte_exact_slices_of_a_real_original": 10,
                              "passages_checked": 10},
        "repetition": {"ours_already_quoted_bodies_injected": 0,
                       "stock_already_quoted_bodies_injected": 17},
        "cost_of_buying_replay_by_caching": {"ours_receipt_bytes": 100,
                                             "stock_cached_material_bytes": 10},
    }
    bounds = {
        "cannot_be_trained_into_silence": {"ours_states_reaching_zero": 0,
                                           "naive_states_reaching_zero": 7},
        "can_promote_as_well_as_suppress": {"ours_range": [0.5, 1.5],
                                            "naive_range": [0.0, 1.0]},
        "hostile_payloads_cannot_raise_attention": [{"neutralised": True}] * 8,
    }
    cost = {"recall_injection": {"live_chars_per_turn": {"median": 757.0}}}
    built = rows(guarantees, bounds, cost)
    assert len(built) == 11
    assert all(len(row) == 3 for row in built)
