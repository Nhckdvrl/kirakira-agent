"""Consolidate frozen memory runs without dropping failures or pooling protocols."""

import argparse
import json
import sqlite3
import statistics
from collections import Counter
from contextlib import closing
from pathlib import Path


def summarize(rows):
    answerable = [row for row in rows if row["category"] != "abstention"]
    labeled = [row["labeled_message_recall"] for row in answerable if row["labeled_message_recall"] is not None]
    correct = sum(row.get("judge_valid") is True and row.get("correct") is True for row in rows)
    return {
        "trials": len(rows), "correct": correct, "accuracy": correct / len(rows) if rows else None,
        "invalid_or_missing_judgments": sum(row.get("judge_valid") is not True for row in rows),
        "reader_truncated": sum(row.get("finish_reason") == "length" for row in rows),
        "answerable_trials": len(answerable),
        "supporting_session_recall": statistics.mean(row["session_recall"] for row in answerable) if answerable else None,
        "labeled_source_recall": statistics.mean(labeled) if labeled else None,
        "labeled_source_trials": len(labeled),
        "citation_ids_valid": sum(row.get("citations_valid") is True for row in rows),
        "answers_with_citations": sum(bool(row.get("citations")) for row in rows),
        "mean_material_chars": statistics.mean(row["material_chars"] for row in rows) if rows else 0,
    }


def systems(rows):
    return {name: summarize([row for row in rows if row["system"] == name])
            for name in sorted({row["system"] for row in rows})}


def model_usage(directory):
    path = directory / "reader-runtime/model-registry.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT state,usage_json FROM model_calls").fetchall()
        endpoints = [row[0] for row in db.execute("SELECT base_url FROM model_connections WHERE provider != 'local-embedding'")]
    values = [json.loads(row[1]) for row in rows if row[1]]
    return {"business_model_calls": len(rows), "states": dict(Counter(row[0] for row in rows)),
            "endpoints": endpoints, "input_tokens": sum(v.get("input_tokens") or 0 for v in values),
            "output_tokens": sum(v.get("output_tokens") or 0 for v in values),
            "coverage": "native chat-call ledger; setup probes and local embeddings are separate"}


def read_run(directory, *, complete):
    manifest = json.loads((directory / "manifest.json").read_text())
    result = json.loads((directory / "result.json").read_text())
    expected = {(qid, name) for qid in manifest["question_ids"] for name in manifest["systems"]}
    identities = [(row["question_id"], row["system"]) for row in result["rows"]]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate experiment trials")
    if complete and set(identities) != expected:
        raise ValueError("primary experiment is incomplete: " + directory.name)
    usage = model_usage(directory)
    if complete and usage["states"].get("started", 0):
        raise ValueError("primary experiment still has active calls")
    return {"name": directory.name, "manifest": manifest, "builds": result["builds"],
            "rows": result["rows"], "model_usage": usage, "complete": set(identities) == expected,
            "summary": systems(result["rows"])}


def consolidate(directories, exploratory=()):
    runs = [read_run(path, complete=True) for path in directories]
    if not runs:
        raise ValueError("at least one primary run is required")
    reference = runs[0]["manifest"]
    seen, rows = set(), []
    for run in runs:
        manifest = run["manifest"]
        for field in ("dataset_sha256", "model_config", "embedding_manifest_sha256", "max_chars", "candidate_budget", "systems", "reader_format"):
            if manifest[field] != reference[field]:
                raise ValueError("incompatible primary experiment field: " + field)
        current = set(manifest["question_ids"])
        if seen & current:
            raise ValueError("primary partitions contain overlapping question IDs")
        seen.update(current)
        rows.extend(run["rows"])
    paired = {}
    for subject, baseline in (("akasha", "dense"), ("akasha-fused", "dense"),
                              ("akasha-fused-excerpts", "dense-excerpts"),
                              ("akasha-fused-excerpts", "akasha")):
        left = {r["question_id"]: bool(r.get("judge_valid") and r.get("correct")) for r in rows if r["system"] == subject}
        right = {r["question_id"]: bool(r.get("judge_valid") and r.get("correct")) for r in rows if r["system"] == baseline}
        if left.keys() != right.keys():
            continue
        paired[subject + " vs " + baseline] = {
            "trials": len(left), "wins": sum(left[k] and not right[k] for k in left),
            "losses": sum(right[k] and not left[k] for k in left),
            "both_correct": sum(left[k] and right[k] for k in left),
            "both_incorrect": sum(not left[k] and not right[k] for k in left)}
    return {"questions": len(seen), "primary_runs": runs, "summary": systems(rows),
            "by_category": {kind: systems([row for row in rows if row["category"] == kind])
                            for kind in sorted({row["category"] for row in rows})},
            "paired": paired, "exploratory_runs": [read_run(path, complete=False) for path in exploratory],
            "limits": ["stratified public subset, not the complete benchmark or a leaderboard",
                       "same configured reader and judge; one reader sample per case; no cross-model quality claim",
                       "source-ID coverage does not establish that an excerpt contains the answer",
                       "valid citation IDs do not establish that every claim is supported",
                       "question-ID partitions may share background histories",
                       "invalid judgments remain in the denominator as failures"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", type=Path, required=True)
    parser.add_argument("--exploratory", nargs="*", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = consolidate(args.runs, args.exploratory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"questions": report["questions"], "summary": report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
