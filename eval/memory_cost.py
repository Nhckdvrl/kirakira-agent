"""Akashic's public 272-turn mechanism/cost protocol on Kirakira's pinned cycle.

Inspired by Akashic scripts/akasha_cost_scenario.py (MIT, d85b00c).
Synthetic vectors measure mechanism and cost, never semantic answer quality.
Run independent processes serially with distinct PYTHONHASHSEED values.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import resource
import sqlite3
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"

import numpy as np

import plugins.akasha2.application.cycle as cycle_module
import plugins.akasha2.infrastructure.persistence as persistence
from plugins.akasha2.application.cycle import MemoryCycle
from plugins.akasha2.domain.model import ContextState, MemoryConfig, Turn, TurnFeedback
from plugins.akasha2.infrastructure.consumption import Applied, Consumption, Skipped


def distribution(values):
    ordered = sorted(values)
    return {
        "n": len(values),
        "p50": ordered[len(ordered) // 2],
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "min": ordered[0],
        "max": ordered[-1],
    }


def rss():
    return {
        "current_mib": int(Path("/proc/self/statm").read_text().split()[1])
        * os.sysconf("SC_PAGE_SIZE")
        / 2**20,
        "peak_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
    }


def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    rng = np.random.default_rng(20260929)
    prototypes = rng.normal(size=(9, args.dimensions)).astype(np.float32)
    turns, entries = [], []
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for node in range(args.turns):
        vector = prototypes[node % 9] + rng.normal(0, 0.07, args.dimensions).astype(
            np.float32
        )
        vector /= np.linalg.norm(vector)
        gap = 86400.0 if node % 37 == 0 else float(node % 11 + 1)
        stamp += timedelta(seconds=gap)
        feedback = TurnFeedback()
        if node > 9 and node % 19 == 0:
            feedback = TurnFeedback(remember_nodes=(node - 9,), remember_boost=2.0)
        if node > 12 and node % 23 == 0:
            feedback = TurnFeedback(forget_nodes=(node - 12,))
        session, user, assistant = f"scenario:{node % 3}", f"u:{node}", f"a:{node}"
        turns.append(
            Turn(
                node,
                f"turn:{node}",
                session,
                node * 2,
                user,
                assistant,
                stamp.isoformat(),
                stamp.isoformat(),
                f"episode {node % 9}",
                "response",
                None if node > 0 and node % 17 == 0 else vector,
                None if node > 0 and node % 13 == 0 else vector.copy(),
                ((f"topic{node % 9}", 2), ("shared", 1)),
                (("reply", 1),),
                gap,
                feedback,
            )
        )
        entries.append(
            Applied(
                learning_binding="scenario-binding",
                session_id=session,
                ending=(node * 2 + 1, assistant),
                members=((node * 2, user), (node * 2 + 1, assistant)),
                observations=(),
                source_digest=hashlib.sha256(str(node).encode()).hexdigest(),
            )
        )
    config = MemoryConfig()
    manifest = {
        "protocol": "akashic d85b00c public cost scenario; Kirakira pure-cycle/persistence driver",
        "turns": args.turns,
        "dimensions": args.dimensions,
        "seed": 20260929,
        "python_hash_seed": os.getenv("PYTHONHASHSEED"),
        "python": sys.version,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "pydantic", "jieba")
        },
        "blas": np.__config__.show(mode="dicts"),
        "blas_threads": 1,
        "input_sha256": hashlib.sha256(
            json.dumps(
                [asdict(t) for t in turns], sort_keys=True, default=lambda v: v.tolist()
            ).encode()
        ).hexdigest(),
        "source_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), *sorted(Path("plugins/akasha2").rglob("*.py"))]
        },
        "limitations": [
            "synthetic vectors, not semantic retrieval quality",
            "pure cycle/persistence, not whole Runtime",
            "finite same-platform evidence, not universal equivalence",
        ],
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))

    def export(path, cycle, progress, previous=None):
        return persistence.write_memory_database(
            path,
            turns=cycle.turns,
            graph=cycle.graph,
            events=cycle.events,
            evidence=cycle.evidence,
            captures=[],
            context=cycle.context,
            burst_members=cycle.burst_members,
            config=config,
            metadata={},
            recalls=cycle.recalls,
            consumption=progress,
            previous_consumption=previous,
        )

    def restore(path, values):
        graph, events, evidence, context, recalls, bursts = (
            persistence.load_memory_state(path, turns=values, config=config)
        )
        return MemoryCycle.restore(
            config=config,
            turns=values,
            graph=graph,
            context=context,
            events=events,
            evidence=evidence,
            recalls=recalls,
            burst_members=bursts,
        )

    def tables(path):
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            schema = db.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            return schema, {
                name: sorted(db.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
                for kind, name, _ in schema
                if kind == "table"
            }

    started = time.perf_counter()
    baseline = MemoryCycle(config)
    baseline.context = ContextState((), None, ())
    progress = Consumption(cutover_heads=())
    learning = []
    for turn, entry in zip(turns, entries, strict=True):
        clock = time.perf_counter()
        baseline.commit(turn, None)
        learning.append(time.perf_counter() - clock)
        progress = progress.append(entry)
    build_seconds = time.perf_counter() - started
    path, full_path = args.output / "memory.db", args.output / "full.db"
    export(full_path, baseline, progress)
    expected_hash = persistence.logical_state_sha256(full_path)
    ready_rss = rss()
    del baseline

    cycle = MemoryCycle(config)
    cycle.context = ContextState((), None, ())
    state = Consumption(cutover_heads=())
    for turn, entry in zip(turns[:-16], entries[:-16], strict=True):
        cycle.commit(turn, None)
        state = state.append(entry)
    export(path, cycle, state)
    publication, recovery = [], []
    for turn, entry in zip(turns[-16:], entries[-16:], strict=True):
        cycle.commit(turn, None)
        next_state = state.append(entry)
        clock = time.perf_counter()
        export(path, cycle, next_state, state)
        publication.append(time.perf_counter() - clock)
        state = next_state
        export(args.output / "reference.db", cycle, state)
        assert tables(path) == tables(args.output / "reference.db")
        clock = time.perf_counter()
        cycle = restore(path, cycle.turns)
        recovery.append(time.perf_counter() - clock)
    assert persistence.logical_state_sha256(path) == expected_hash

    cue = replace(
        turns[-1], node_id=args.turns, turn_id="query", feedback=TurnFeedback()
    )
    before = export(args.output / "before.db", cycle, state)
    recalls, query_seconds = [], []
    for _ in range(5):
        for index in range(9):
            clock = time.perf_counter()
            result = cycle.retrieve(replace(cue, user_dense=prototypes[index]))
            query_seconds.append(time.perf_counter() - clock)
            recalls.append(asdict(result.completion))
    original = cycle_module.read_pattern_completion

    def fail(**kwargs):
        raise RuntimeError("injected interrupted readout")

    cycle_module.read_pattern_completion = fail
    try:
        try:
            cycle.retrieve(cue)
        except RuntimeError as error:
            assert str(error) == "injected interrupted readout"
        else:
            raise AssertionError("readout did not fail")
    finally:
        cycle_module.read_pattern_completion = original
    assert export(args.output / "after.db", cycle, state) == before

    faults = []
    for boundary in ("file-sync", "replace", "directory-sync"):
        original_sync, original_replace = persistence.os.fsync, persistence.os.replace
        count = 0
        next_state = state.mark_skipped(
            Skipped(
                session_id="fault",
                ending=(len(faults), f"fault:{boundary}"),
                reason="scenario",
            )
        )

        def sync(fd):
            nonlocal count
            count += 1
            if (boundary == "file-sync" and count == 1) or (
                boundary == "directory-sync" and count == 2
            ):
                raise OSError("injected " + boundary)
            original_sync(fd)

        def publish(source, target):
            if boundary == "replace":
                raise OSError("injected " + boundary)
            original_replace(source, target)

        persistence.os.fsync, persistence.os.replace = sync, publish
        try:
            try:
                export(path, cycle, next_state, state)
            except OSError as error:
                assert str(error) == "injected " + boundary
            else:
                raise AssertionError("publication did not fail")
        finally:
            persistence.os.fsync, persistence.os.replace = (
                original_sync,
                original_replace,
            )
        assert persistence.load_consumption(path) == (
            next_state if boundary == "directory-sync" else state
        )
        tables(path)
        state = persistence.load_consumption(path)
        cycle = restore(path, cycle.turns)
        faults.append(
            {"boundary": boundary, "published_new": boundary == "directory-sync"}
        )
    result = {
        "logical_state_sha256": expected_hash,
        "recalls_sha256": hashlib.sha256(
            json.dumps(recalls, sort_keys=True).encode()
        ).hexdigest(),
        "nine_queries_sha256": hashlib.sha256(
            json.dumps(recalls[:9], sort_keys=True).encode()
        ).hexdigest(),
        "turns": args.turns,
        "dimensions": args.dimensions,
        "full_rebuild_seconds": build_seconds,
        "learning_seconds": distribution(learning),
        "query_seconds": distribution(query_seconds),
        "publication_seconds": distribution(publication),
        "recovery_seconds": distribution(recovery),
        "ready_rss": ready_rss,
        "final_rss": rss(),
        "database_mib": path.stat().st_size / 2**20,
        "recovery_count": 19,
        "all_tables_equal": True,
        "query_unchanged": True,
        "faults": faults,
    }
    (args.output / "result.json").write_text(json.dumps(result, indent=2))
    (args.output / "receipt.json").write_text(
        json.dumps(
            {
                name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
                for name in ("manifest.json", "result.json")
            },
            indent=2,
        )
    )
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--turns", type=int, default=272)
    parser.add_argument("--dimensions", type=int, default=33)
    args = parser.parse_args()
    if args.turns < 32 or args.dimensions < 1:
        parser.error("turns >= 32 and dimensions >= 1 required")
    run(args)


if __name__ == "__main__":
    main()
