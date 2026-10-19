"""Read the exact frozen learned graphs with the corrected host readout.

This is a diagnostic on already-observed questions, never a new holdout.
Original raw-pair formatting and budgets remain identical across controls.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from core.memory.engine import MemoryQuery
from plugins.akasha2.engine import Akasha2MemoryEngine
from plugins.akasha2.infrastructure.persistence import logical_state_sha256
from plugins.default_memory.config_models import (
    Config,
    MemoryConfig,
    MemoryEmbeddingConfig,
)

from .dataset import load_longmemeval
from .offline_ab import _turn_groups
from .prepare_retrieval import CachedEmbedder, bounded
from .protocol import chronological, timestamp


async def run(args):
    if args.output.exists():
        raise FileExistsError("use a new readout experiment output")
    instances = {case.question_id: case for case in load_longmemeval(args.dataset)}
    cache = CachedEmbedder(args.embeddings)
    config = Config(
        model="unused",
        memory=MemoryConfig(
            embedding=MemoryEmbeddingConfig(
                model=cache.model_id,
                base_url="offline://no-network",
                output_dimensionality=384,
            )
        ),
    )
    checks = []
    for row in map(json.loads, args.retrieval.read_text().splitlines()):
        case = instances[row["question_id"]]
        lookup, positions = {}, {}
        for date, sid, messages in chronological(case):
            for group in _turn_groups(messages):
                position = positions.get(sid, 0)
                positions[sid] = position + len(group)
                if len(group) == 2 and [item.role for item in group] == [
                    "user",
                    "assistant",
                ]:
                    identity = hashlib.sha256(
                        f"{case.question_id}:{sid}:{position}".encode()
                    ).hexdigest()
                    lookup[identity] = {
                        "source_session": sid,
                        "summary": f"[{date}] user: {group[0].content}\nassistant: {group[1].content}",
                    }
        work = args.cases / case.question_id
        before = logical_state_sha256(work / "memory/akasha2.db")
        engine = Akasha2MemoryEngine(
            config=config,
            workspace=work,
            http_resources=SimpleNamespace(),
            embedder=cache,
        )
        try:
            result = await engine.query(
                MemoryQuery(
                    case.question,
                    limit=10,
                    effect="read_only",
                    timestamp=timestamp(row["index_time"]),
                )
            )
            records = [
                {
                    "id": record.id,
                    "score": record.score,
                    "signals": record.signals,
                    **lookup[record.evidence[0].refs[0]],
                }
                for record in result.records
            ]
            context, kept = bounded(records)
            gold = set(case.answer_session_ids)
            found = {record["source_session"] for record in kept}
            row["arms"] = {
                "akasha2_dual_lane": {
                    "context": context,
                    "records": kept,
                    "trace": result.trace,
                    "evidence_session_recall": len(gold & found) / len(gold)
                    if gold
                    else None,
                }
            }
        finally:
            await engine.aclose()
        after = logical_state_sha256(work / "memory/akasha2.db")
        assert before == after, "readout changed frozen learned state"
        checks.append(
            {"question_id": case.question_id, "state_sha256": before, "unchanged": True}
        )
        with args.output.open("a") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    args.output.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "kind": "observed-question readout diagnostic, not holdout",
                "readout_source": "Akashic d85b00c plugins/akasha/recalls.py::select_hits",
                "limit_difference": "Kirakira uses total limit=10; upstream has per-completion limit plus dense",
                "old_retrieval_sha256": hashlib.sha256(
                    args.retrieval.read_bytes()
                ).hexdigest(),
                "script_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "source_sha256": {
                    str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (
                        Path("plugins/akasha2/engine.py"),
                        Path("plugins/akasha2/selection.py"),
                    )
                },
                "checks": checks,
                "max_pairs": 10,
                "max_chars": 24000,
            },
            indent=2,
        )
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--embeddings", type=Path, required=True)
    p.add_argument("--retrieval", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
