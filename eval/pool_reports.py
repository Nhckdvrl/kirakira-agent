"""Pool disjoint runs of the same suite and re-test, without re-running a reader.

Growing a sample happens in passes: a first subset gets published, a question
turns up in it, and a second disjoint subset is drawn to check that question on
data which had no part in raising it. Both halves then have to be read
together, and by hand that is where a sample quietly becomes a cherry-pick --
so the pooling is a command with its own rules rather than a spreadsheet.

Two rules it enforces, because both are easy to get wrong and neither is
visible in the output otherwise:

* the inputs must not overlap, or shared questions are counted twice;
* a question answered by only one of the two runs is dropped, since a paired
  test needs both arms on the same question.

It reads saved reports and recomputes; it never calls a model.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from eval.significance import paired_comparison, wilson_interval


def load_rows(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = document.get("rows")
    if not rows:
        raise ValueError(f"{path} carries no rows")
    return rows


def scores(rows: list[dict], system: str) -> dict[str, bool]:
    return {row["question_id"]: bool(row.get("correct"))
            for row in rows if row["system"] == system}


def failed_questions(rows: list[dict]) -> set[str]:
    """Questions where either arm never reached the model.

    A transient network failure is recorded as an incorrect answer, and the
    harness refuses to replace a stored failure -- which is the right call,
    since retrying only the trials that went badly is how a result gets
    massaged. The sensitivity check is the honest alternative: report the
    comparison again with those questions dropped, and let both numbers stand.
    """
    return {row["question_id"] for row in rows if row.get("error")}


def compare(ours: dict[str, bool], theirs: dict[str, bool]) -> dict[str, object]:
    shared = sorted(set(ours) & set(theirs))
    first = {key: ours[key] for key in shared}
    second = {key: theirs[key] for key in shared}
    result = paired_comparison(first, second)
    only_first, only_second = result["only_first"], result["only_second"]
    total = result["paired_questions"] or 1
    difference = (only_first - only_second) / total
    error = math.sqrt(only_first + only_second) / total
    low, high = wilson_interval(sum(first.values()), len(first))
    result.update({
        "first_correct": sum(first.values()), "second_correct": sum(second.values()),
        "first_wilson_95": [round(low, 4), round(high, 4)],
        "difference_points": round(difference * 100, 2),
        "difference_95_points": [round((difference - 1.96 * error) * 100, 2),
                                 round((difference + 1.96 * error) * 100, 2)],
    })
    return result


def pool(paths: list[Path], first: str, second: str) -> dict[str, object]:
    per_run, rows = [], []
    for path in paths:
        current = load_rows(path)
        identities = {row["question_id"] for row in current}
        for earlier in per_run:
            overlap = identities & earlier["question_ids"]
            if overlap:
                raise ValueError(
                    f"{path} shares {len(overlap)} questions with {earlier['path']}; "
                    "pooling overlapping runs would count them twice")
        per_run.append({"path": str(path), "questions": len(identities),
                        "question_ids": identities})
        rows.extend(current)
    for entry in per_run:
        entry.pop("question_ids")

    ours, theirs = scores(rows, first), scores(rows, second)
    categories = {row["question_id"]: row["category"] for row in rows}
    failed = failed_questions(rows)
    # A question only one arm reached cannot enter a paired test.
    unpaired = sorted(set(ours) ^ set(theirs))
    answerable = {key for key, kind in categories.items() if kind != "abstention"}

    by_category = {}
    for kind in sorted(set(categories.values())):
        chosen = {key for key, value in categories.items() if value == kind}
        by_category[kind] = compare({k: v for k, v in ours.items() if k in chosen},
                                    {k: v for k, v in theirs.items() if k in chosen})
    return {
        "kind": "pooled re-test of disjoint runs, no reader or judge calls",
        "systems": {"first": first, "second": second},
        "runs": per_run,
        "questions_answered_by_only_one_run": len(unpaired),
        "answerable_only": compare({k: v for k, v in ours.items() if k in answerable},
                                   {k: v for k, v in theirs.items() if k in answerable}),
        "transport_failures": {
            "questions_where_either_arm_failed": len(failed),
            "rows_with_error": sum(1 for row in rows if row.get("error")),
            "note": ("网络失败被记为答错，而且 --resume 不允许替换已存的失败"
                     "（只重跑失败项正是结果被修饰的方式）。因此这里并列给出剔除这些题后的结果。"),
        },
        "answerable_excluding_transport_failures": compare(
            {k: v for k, v in ours.items() if k in answerable and k not in failed},
            {k: v for k, v in theirs.items() if k in answerable and k not in failed}),
        "all_questions": compare(ours, theirs),
        "by_category": by_category,
        "note": ("剔除 abstention 后再读主结果：不给历史的系统天然会弃答，"
                 "把弃答题混进总分会让它虚高。"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--first", default="akasha-fused-excerpts")
    parser.add_argument("--second", default="dense-excerpts")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = pool(args.reports, args.first, args.second)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
