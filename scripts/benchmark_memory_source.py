"""Compare source IO on identical frozen graphs, verifying records/state parity."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import types
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from agent.config_models import Config, MemoryConfig, MemoryEmbeddingConfig
from core.memory.engine import MemoryQuery
from eval.longmemeval.prepare_retrieval import CachedEmbedder
from eval.longmemeval.protocol import timestamp
from plugins.akasha2.engine import Akasha2MemoryEngine
from plugins.akasha2.infrastructure.persistence import logical_state_sha256


def baseline(commit):
    def load(name, package, path):
        content = subprocess.check_output(["git", "show", f"{commit}:{path}"])
        module = types.ModuleType(name)
        module.__package__ = package
        sys.modules[name] = module
        exec(compile(content, path, "exec"), module.__dict__)
        return module, hashlib.sha256(content).hexdigest()

    source, source_hash = load(
        "session._source_benchmark", "session", "session/turn_source.py"
    )
    engine, engine_hash = load(
        "plugins.akasha2._source_benchmark",
        "plugins.akasha2",
        "plugins/akasha2/engine.py",
    )
    engine.load_committed_turn_messages = source.load_committed_turn_messages
    return engine.Akasha2MemoryEngine, {
        "session/turn_source.py": source_hash,
        "plugins/akasha2/engine.py": engine_hash,
    }


async def run(args):
    if args.output.exists():
        raise FileExistsError("use a fresh benchmark output")
    old, hashes = baseline(args.baseline)
    rows = [json.loads(s) for s in args.retrieval.read_text().splitlines()]
    rows = sorted(rows, key=lambda r: (r["turns"], r["question_id"]))
    selected = [rows[i] for i in sorted({0, len(rows) // 2, len(rows) - 1})]
    embedder = CachedEmbedder(args.embeddings)
    config = Config(
        model="unused",
        memory=MemoryConfig(
            embedding=MemoryEmbeddingConfig(
                model=embedder.model_id,
                base_url="offline://no-network",
                output_dimensionality=384,
            )
        ),
    )
    real_connect = sqlite3.connect
    count = 0

    def counted(path, *a, **kw):
        nonlocal count
        if "sessions.db" in str(path):
            count += 1
        return real_connect(path, *a, **kw)

    results = []
    sqlite3.connect = counted
    try:
        for row in selected:
            path = args.cases / row["question_id"]
            before = logical_state_sha256(path / "memory/akasha2.db")
            source_before = hashlib.sha256(
                (path / "sessions.db").read_bytes()
            ).hexdigest()
            expected = None
            max_score_delta = 0.0
            arms = {}
            for name, cls in [("baseline", old), ("snapshot", Akasha2MemoryEngine)]:
                restorations = []
                queries = []
                restore_io = []
                query_io = []
                for repeat in range(args.repeats):
                    c = count
                    start = time.perf_counter()
                    engine = cls(
                        config=config,
                        workspace=path,
                        http_resources=SimpleNamespace(),
                        embedder=embedder,
                    )
                    restorations.append(time.perf_counter() - start)
                    restore_io.append(count - c)
                    try:
                        for _ in range(5):
                            c = count
                            start = time.perf_counter()
                            answer = await engine.query(
                                MemoryQuery(
                                    row["question"],
                                    limit=10,
                                    effect="read_only",
                                    timestamp=timestamp(row["index_time"]),
                                )
                            )
                            queries.append(time.perf_counter() - start)
                            query_io.append(count - c)
                            records = json.dumps(
                                [asdict(r) for r in answer.records],
                                sort_keys=True,
                                ensure_ascii=False,
                            )
                            if expected is None:
                                expected = records
                            original_records = json.loads(expected)
                            current_records = json.loads(records)
                            deltas = [
                                abs(left["score"] - right["score"])
                                for left, right in zip(
                                    original_records, current_records, strict=True
                                )
                            ]
                            if deltas:
                                max_score_delta = max(max_score_delta, max(deltas))
                            for left, right in zip(
                                original_records, current_records, strict=True
                            ):
                                left_score, right_score = (
                                    left.pop("score"),
                                    right.pop("score"),
                                )
                                assert left == right, (
                                    "source identity, content, lane or order changed"
                                )
                                assert np.isclose(
                                    left_score,
                                    right_score,
                                    atol=8 * np.finfo(float).eps,
                                    rtol=8 * np.finfo(float).eps,
                                ), "scores changed beyond floating-point roundoff"
                    finally:
                        await engine.aclose()
                arms[name] = {
                    "restore_seconds": restorations,
                    "query_seconds": queries,
                    "source_connections_per_restore": restore_io,
                    "source_connections_per_query": query_io,
                    "restore_p50_p95": np.percentile(restorations, [50, 95]).tolist(),
                    "query_p50_p95": np.percentile(queries, [50, 95]).tolist(),
                }
            assert logical_state_sha256(path / "memory/akasha2.db") == before
            assert (
                hashlib.sha256((path / "sessions.db").read_bytes()).hexdigest()
                == source_before
            )
            results.append(
                {
                    "question_id": row["question_id"],
                    "turns": row["turns"],
                    "arms": arms,
                    "evidence_and_order_equal": True,
                    "scores_exact": max_score_delta == 0.0,
                    "maximum_score_difference": max_score_delta,
                    "learned_state_unchanged": True,
                    "source_bytes_unchanged": True,
                    "record_sha256": hashlib.sha256(expected.encode()).hexdigest(),
                }
            )
    finally:
        sqlite3.connect = real_connect
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "baseline_commit": args.baseline,
                "baseline_source_sha256": hashes,
                "current_source_sha256": {
                    p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in hashes
                },
                "results": results,
                "scope": "3 frozen graphs, cached vectors, serial on this machine; baseline always measured first, no universal speedup claim",
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {"cases": len(results), "records_equal": True, "state_unchanged": True}
        )
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--retrieval", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--embeddings", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--baseline", default="acbe0b9850def2d59256a075afcdf9f09d1109aa")
    p.add_argument("--repeats", type=int, default=3)
    args = p.parse_args()
    if args.repeats < 1:
        p.error("repeats must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
