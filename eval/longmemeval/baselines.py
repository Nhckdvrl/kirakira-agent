"""Fixed BM25, dense/BM25 RRF and recency controls on the same original pairs.

No gold labels enter indexing, scoring, ranking or context construction. Gold
pairs are used only after ranking to measure exact evidence coverage. Reuses
frozen graph-arm outputs without re-running or changing their algorithms.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import numpy as np

from .dataset import load_longmemeval
from .offline_ab import _turn_groups
from .prepare_retrieval import CachedEmbedder, bounded
from .protocol import chronological


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(args):
    if args.output.exists():
        raise FileExistsError("use a new retrieval output")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cache = CachedEmbedder(args.embeddings)
    frozen = {
        row["question_id"]: row
        for row in map(json.loads, args.graph_retrieval.read_text().splitlines())
    }
    compact = []
    instances = {case.question_id: case for case in load_longmemeval(args.dataset)}
    for identity in frozen:
        case = instances[identity]
        row = dict(frozen[case.question_id])
        turns, gold, positions, legacy_ids = [], set(), {}, {}
        for date, sid, messages in chronological(case):
            for group in _turn_groups(messages):
                position = positions.get(sid, 0)
                positions[sid] = position + len(group)
                if len(group) != 2 or [t.role for t in group] != ["user", "assistant"]:
                    continue
                identity = hashlib.sha256(
                    f"{case.question_id}:{sid}:{position}".encode()
                ).hexdigest()
                legacy_ids[f"lme:{sid}:{position}"] = identity
                turns.append(
                    {
                        "id": identity,
                        "source_session": sid,
                        "summary": f"[{date}] user: {group[0].content}\nassistant: {group[1].content}",
                        "texts": [t.content for t in group],
                    }
                )
                if any(t.has_answer for t in group):
                    gold.add(identity)
        query = cache.vectors["q:" + case.question]
        started = time.perf_counter()
        dense = sorted(
            [
                {
                    **t,
                    "score": max(
                        float(np.dot(query, cache.vectors["p:" + text]))
                        for text in t["texts"]
                    ),
                }
                for t in turns
            ],
            key=lambda t: t["score"],
            reverse=True,
        )
        dense_seconds = time.perf_counter() - started
        dense_context, dense_kept = bounded(dense)
        assert dense_context == row["arms"]["dense"]["context"], (
            "projection or dense ranking changed"
        )
        assert [t["id"] for t in dense_kept] == [
            t["id"] for t in row["arms"]["dense"]["records"]
        ]
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE VIRTUAL TABLE documents USING fts5(content)")
            db.executemany(
                "INSERT INTO documents(rowid,content) VALUES (?,?)",
                [(i + 1, "\n".join(t["texts"])) for i, t in enumerate(turns)],
            )
            expression = " OR ".join(
                '"' + term + '"'
                for term in sorted(set(re.findall(r"\w+", case.question)))
            )
            started = time.perf_counter()
            hits = (
                db.execute(
                    "SELECT rowid,bm25(documents) AS score FROM documents WHERE documents MATCH ? ORDER BY score,rowid",
                    (expression,),
                ).fetchall()
                if expression
                else []
            )
            bm25 = [{**turns[index - 1], "score": -score} for index, score in hits]
            bm25_seconds = time.perf_counter() - started
        lookup = {t["id"]: t for t in turns}
        scores = {}
        started = time.perf_counter()
        for ranking in (dense, bm25):
            for rank, turn in enumerate(ranking, 1):
                scores[turn["id"]] = scores.get(turn["id"], 0.0) + 1 / (60 + rank)
        hybrid = [
            {**lookup[key], "score": score}
            for key, score in sorted(
                scores.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ]
        rrf_seconds = time.perf_counter() - started
        new_arms = {}
        for name, records, seconds in (
            ("bm25", bm25, bm25_seconds),
            ("rrf", hybrid, dense_seconds + bm25_seconds + rrf_seconds),
            ("recency", list(reversed(turns)), 0.0),
        ):
            context, kept = bounded(records)
            found = {t["source_session"] for t in kept}
            sessions = set(case.answer_session_ids)
            new_arms[name] = {
                "context": context,
                "records": [{k: v for k, v in t.items() if k != "texts"} for t in kept],
                "retrieval_seconds": seconds,
                "evidence_session_recall": len(found & sessions) / len(sessions)
                if sessions
                else None,
            }
        row["arms"] = new_arms
        with args.output.open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        coverage = {}
        # Evaluate frozen graph controls and new controls with the same gold set.
        for arm, result in {**frozen[case.question_id]["arms"], **new_arms}.items():
            ids = [record["id"] for record in result["records"]]
            normalized = [
                value if value in lookup else legacy_ids[value] for value in ids
            ]
            assert all(
                lookup[identity]["summary"] == record["summary"]
                for identity, record in zip(normalized, result["records"], strict=True)
            ), "source identity mismatch"
            found_gold = set(normalized) & gold
            coverage[arm] = {
                "gold_pairs": len(gold),
                "retrieved_pairs": len(ids),
                "gold_found": len(found_gold),
                "exact_pair_recall": len(found_gold) / len(gold)
                if gold and not case.question_id.endswith("_abs")
                else None,
                "all_gold_pairs": gold <= set(normalized) if gold else None,
                "reciprocal_rank": next(
                    (
                        1 / (rank + 1)
                        for rank, identity in enumerate(normalized)
                        if identity in gold
                    ),
                    0.0,
                )
                if gold
                else None,
                "evidence_session_recall": result.get("evidence_session_recall"),
                "retrieval_seconds": result.get("retrieval_seconds"),
                "source_ids": normalized,
            }
        compact.append(
            {
                "question_id": case.question_id,
                "question_type": case.question_type,
                "abstention": case.question_id.endswith("_abs"),
                "arms": coverage,
            }
        )
    args.output.with_suffix(".coverage.json").write_text(json.dumps(compact, indent=2))
    args.output.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "dataset_sha256": digest(args.dataset),
                "embedding_sha256": digest(args.embeddings),
                "graph_retrieval_sha256": digest(args.graph_retrieval),
                "retrieval_sha256": digest(args.output),
                "script_sha256": digest(Path(__file__)),
                "cases": len(compact),
                "context_chars": 24000,
                "max_pairs": 10,
                "rrf_k": 60,
                "bm25": "SQLite FTS5 BM25 defaults, OR of unique query tokens; rowid tie-break",
                "recency": "last ten complete chronological original pairs",
                "limitations": [
                    "retrieval baselines, not Mem0/Zep product evaluations",
                    "same fixed 36 cases; no tuning on results",
                    "cached embedding lookup excluded from timing",
                ],
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--embeddings", type=Path, required=True)
    parser.add_argument("--graph-retrieval", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    prepare(parser.parse_args())


if __name__ == "__main__":
    main()
