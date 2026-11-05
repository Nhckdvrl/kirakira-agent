"""Run every model-free memory measurement and print one comparable table.

No API key, no chat model, no private workspace: fixed vectors and the real
Ledger, Learning, MessageConsumer and recall components. Anyone with the
repository can reproduce these numbers.

Each measurement runs several times because a single run varies a lot on slow
storage, and medians with observed ranges are reported rather than one sample.

This bounds cost only. Whether recall still answers well is a different
question, measured by `eval.memory_depth` with a real model; a cheaper run is
not evidence of an equally useful one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

from eval import memory_cost, recall_cost


def _span(values: list[float]) -> dict[str, float]:
    return {"median": round(statistics.median(values), 4),
            "min": round(min(values), 4), "max": round(max(values), 4),
            "runs": len(values)}


async def collect(*, runs: int, seed_turns: int, live_turns: int, filler_chars: int,
                  learn_turns: int, queries: int) -> dict[str, object]:
    recall_chars: list[float] = []
    recall_total: list[float] = []
    index_sources: list[float] = []
    body_sources: list[float] = []
    for _ in range(runs):
        measured = await recall_cost.measure(
            seed_turns=seed_turns, live_turns=live_turns, filler_chars=filler_chars)
        report = measured.report()
        recall_chars.append(report["injection"]["mean_live_chars_per_turn"])
        recall_total.append(report["injection"]["total_live_chars"])
        index_sources.append(report["form"]["mean_index_sources_per_turn"])
        body_sources.append(report["form"]["mean_body_sources_per_turn"])

    learn_s: list[float] = []
    query_ms: list[float] = []
    peak_mib: list[float] = []
    for _ in range(runs):
        report = await memory_cost.measure(turns=learn_turns, queries=queries)
        learn_s.append(report["learn"]["seconds"])
        query_ms.append(report["query"]["mean_ms"])
        peak_mib.append(report["learn"]["peak_mib"])

    return {
        "kind": "model-free memory cost measurements",
        "conditions": {
            "runs_per_measurement": runs,
            "recall": {"seed_turns": seed_turns, "live_turns": live_turns,
                       "filler_chars_per_seed_message": filler_chars},
            "engine": {"learn_turns": learn_turns, "queries": queries},
            "chat_model": None, "api_key_required": False,
        },
        "recall_injection": {
            "live_chars_per_turn": _span(recall_chars),
            "total_live_chars": _span(recall_total),
            "body_sources_per_turn": _span(body_sources),
            "index_sources_per_turn": _span(index_sources),
        },
        "engine": {
            "learn_seconds": _span(learn_s),
            "recall_mean_ms": _span(query_ms),
            "learn_peak_mib": _span(peak_mib),
        },
        "caveat": "只衡量开销，不衡量检索质量；更便宜不等于答案同样有用。",
    }


def table(report: dict) -> str:
    recall = report["recall_injection"]
    engine = report["engine"]
    rows = [
        ("每轮实时召回注入（字）", recall["live_chars_per_turn"]),
        ("每轮正文来源（条）", recall["body_sources_per_turn"]),
        ("每轮索引行（条）", recall["index_sources_per_turn"]),
        ("学习耗时（秒）", engine["learn_seconds"]),
        ("单次检索均值（毫秒）", engine["recall_mean_ms"]),
        ("学习峰值分配（MiB）", engine["learn_peak_mib"]),
    ]
    lines = ["| 指标 | 中位数 | 范围 |", "| --- | ---: | ---: |"]
    lines += [f"| {name} | {value['median']} | {value['min']}–{value['max']} |"
              for name, value in rows]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seed-turns", type=int, default=12)
    parser.add_argument("--live-turns", type=int, default=15)
    parser.add_argument("--filler-chars", type=int, default=2000)
    parser.add_argument("--learn-turns", type=int, default=150)
    parser.add_argument("--queries", type=int, default=15)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.runs < 1:
        raise SystemExit("--runs must be at least 1")
    report = asyncio.run(collect(
        runs=args.runs, seed_turns=args.seed_turns, live_turns=args.live_turns,
        filler_chars=args.filler_chars, learn_turns=args.learn_turns, queries=args.queries))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8")
    print(table(report))
    print()
    print(report["caveat"])


if __name__ == "__main__":
    main()
