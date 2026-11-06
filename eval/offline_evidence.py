"""Regenerate every claim that needs no API key, in one command.

The argument for this project does not rest on benchmark accuracy -- that came
out equivalent to plain dense retrieval -- it rests on properties of the
retrieval receipt and bounds on the proactive feedback loop. Those are the
claims an interviewer is entitled to see run, so they should not be spread
across three entry points with three output formats.

This runs all three model-free suites and prints one table keyed to the claims
on `docs/architecture/why-not-off-the-shelf.md`:

* design properties against a stock retrieval memory, and against the same
  stock memory with a per-request cache, which is the obvious rebuttal;
* bounds on the proactive attention weight, swept over the whole rating space;
* what automatic recall costs per turn.

No chat model, no key, no private workspace. Cost and bounds are not quality:
whether recall answers well is measured separately, with a real model, by
`eval.memory` and `eval.memory_depth`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from eval import cost_report, memory_guarantees, proactive_guarantees


def rows(guarantees: dict, bounds: dict, cost: dict) -> list[tuple[str, str, str]]:
    """(claim, ours, the baseline it is measured against)."""
    reproduce = guarantees["reproducibility"]
    confine = guarantees["read_confinement"]
    attribute = guarantees["attribution"]
    verbatim = guarantees["verbatim_fidelity"]
    repeat = guarantees["repetition"]
    replay_cost = guarantees["cost_of_buying_replay_by_caching"]
    silence = bounds["cannot_be_trained_into_silence"]
    reach = bounds["can_promote_as_well_as_suppress"]
    hostile = bounds["hostile_payloads_cannot_raise_attention"]
    injection = cost["recall_injection"]
    return [
        ("索引增长后重渲染逐字节一致",
         reproduce["ours_identical_after_index_growth"],
         f"朴素 {reproduce['stock_identical_after_index_growth']}；"
         f"加缓存 {reproduce['stock_cached_identical_after_index_growth']}（追平）"),
        ("买回重放的存储代价",
         f"回执 {replay_cost['ours_receipt_bytes']:,} 字节",
         f"缓存 {replay_cost['stock_cached_material_bytes']:,} 字节（更省）"),
        ("越权读取被拒",
         f"{confine['ours_refused']}/{confine['illegal_attempts']}",
         f"{confine['stock_refused']}/{confine['illegal_attempts']}（无每请求准入概念）"),
        ("合法读取仍放行（控制组）", confine["ours_allowed_control"], "—"),
        ("召回带可追责来源",
         attribute["ours_receipts_with_a_recorded_cause"],
         f"{attribute['stock_receipts_with_a_recorded_cause']}（不保留每请求记录）"),
        ("材料是原文的逐字节切片",
         f"{verbatim['byte_exact_slices_of_a_real_original']}/{verbatim['passages_checked']}",
         "不适用（未记录区间）"),
        ("请求里已有的原文被重复注入",
         str(repeat["ours_already_quoted_bodies_injected"]),
         str(repeat["stock_already_quoted_bodies_injected"])),
        ("关注权重归零（永久消音）的状态数",
         str(silence["ours_states_reaching_zero"]),
         f"{silence['naive_states_reaching_zero']}（朴素比例策略）"),
        ("关注权重值域",
         str(reach["ours_range"]),
         f"{reach['naive_range']}，上限恰等于中性、只能压不能抬"),
        ("构造 payload 被判为中性",
         f"{sum(row['neutralised'] for row in hostile)}/{len(hostile)}", "—"),
        ("每轮实时召回注入",
         f"{injection['live_chars_per_turn']['median']:.0f} 字（中位）", "—"),
    ]


async def collect(*, runs: int, limit: int, seed_turns: int, live_turns: int,
                  filler_chars: int = 900, learn_turns: int = 60,
                  queries: int = 15) -> dict:
    guarantees = await memory_guarantees.measure(seed_turns=seed_turns, live_turns=live_turns)
    bounds = proactive_guarantees.sweep(limit)
    cost = await cost_report.collect(runs=runs, seed_turns=12, live_turns=15,
                                     filler_chars=filler_chars, learn_turns=learn_turns,
                                     queries=queries)
    return {
        "kind": "every claim that needs no API key, regenerated together",
        "conditions": {"chat_model": None, "api_key_required": False,
                       "guarantee_turns": [seed_turns, live_turns],
                       "rating_states_swept": bounds["conditions"]["rating_states"],
                       "cost_runs": runs},
        "table": [{"claim": claim, "ours": ours, "baseline": baseline}
                  for claim, ours, baseline in rows(guarantees, bounds, cost)],
        "memory_guarantees": guarantees,
        "proactive_bounds": bounds,
        "recall_cost": cost,
        "claim_limits": ("这些是设计性质与开销，不是回答质量。"
                         "公开基准上本项目与朴素向量检索等价，见记忆对照报告。"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--seed-turns", type=int, default=30)
    parser.add_argument("--live-turns", type=int, default=25)
    # The engine section is the only expensive part; keep it tunable so the
    # whole table can be regenerated quickly on a busy machine.
    parser.add_argument("--learn-turns", type=int, default=60)
    parser.add_argument("--queries", type=int, default=15)
    parser.add_argument("--filler-chars", type=int, default=900)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(collect(runs=args.runs, limit=args.limit,
                                 seed_turns=args.seed_turns, live_turns=args.live_turns,
                                 filler_chars=args.filler_chars,
                                 learn_turns=args.learn_turns, queries=args.queries))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8")
    width = max(len(row["claim"]) for row in report["table"])
    print(f"{'性质':{width}s}  {'本项目':>22s}  对照")
    for row in report["table"]:
        print(f"{row['claim']:{width}s}  {row['ours']:>22s}  {row['baseline']}")
    print()
    print(report["claim_limits"])


if __name__ == "__main__":
    main()
