"""Verify frozen reader inputs, original references, and source-only graph rebuild."""

import argparse
import asyncio
import hashlib
import json
import socket
import sqlite3
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from core.memory.engine import MemoryQuery
from eval.longmemeval.prepare_retrieval import CachedEmbedder
from eval.longmemeval.protocol import timestamp
from plugins.akasha2.engine import Akasha2MemoryEngine
from plugins.akasha2.infrastructure.persistence import logical_state_sha256
from plugins.default_memory.config_models import (
    Config,
    MemoryConfig,
    MemoryEmbeddingConfig,
)
from plugins.ledger.manager import SessionManager
from scripts.replay_akasha2 import rebuild


def full_state(path):
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        contract = json.loads(
            db.execute(
                "SELECT value FROM metadata WHERE key='kirakira_checkpoint'"
            ).fetchone()[0]
        )
    return {"learned": logical_state_sha256(path), "contract": contract}


async def run(args):
    if args.output.exists():
        raise FileExistsError("use a fresh verification output")
    originals = {
        r["question_id"]: r
        for r in map(json.loads, args.original_retrieval.read_text().splitlines())
    }
    finals = {
        r["question_id"]: r
        for r in map(json.loads, args.final_retrieval.read_text().splitlines())
    }
    assert originals.keys() == finals.keys()
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
    results = []
    attempts = 0
    original_connect, original_connect_ex = (
        socket.socket.connect,
        socket.socket.connect_ex,
    )

    def no_network(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise AssertionError("offline recovery attempted network IO")

    socket.socket.connect = socket.socket.connect_ex = no_network
    try:
        for identity, row in finals.items():
            for arm, evidence in row["arms"].items():
                assert (
                    evidence["context"] == originals[identity]["arms"][arm]["context"]
                ), "reader inputs changed after indexing upgrade"
            path = args.cases / identity
            graph = path / "memory/akasha2.db"
            expected = full_state(graph)
            sessions = SessionManager(path)
            records = None
            references = []
            try:
                for repeat in range(2):
                    memory = Akasha2MemoryEngine(
                        config=config,
                        workspace=path,
                        http_resources=SimpleNamespace(),
                        embedder=embedder,
                    )
                    try:
                        result = await memory.query(
                            MemoryQuery(
                                row["question"],
                                limit=10,
                                effect="read_only",
                                timestamp=timestamp(row["index_time"]),
                            )
                        )
                        actual = [asdict(record) for record in result.records]
                        if records is not None:
                            assert actual == records, (
                                "restart changed exact recall records"
                            )
                        records = actual
                        if repeat == 0:
                            for record in result.records:
                                evidence = record.evidence[0]
                                fetched = sessions.fetch_messages(
                                    evidence.source_ref, context=0
                                )
                                assert [
                                    m["source_ref"] for m in fetched
                                ] == evidence.refs
                                assert len(fetched) == 2 and all(
                                    m["is_anchor"] and m["content"] in record.summary
                                    for m in fetched
                                )
                                references.append(
                                    {
                                        "record_id": record.id,
                                        "refs": evidence.refs,
                                        "originals_match": True,
                                    }
                                )
                    finally:
                        await memory.aclose()
            finally:
                sessions.close()
            assert full_state(graph) == expected
            source = path / "sessions.db"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            candidate = path / "source-only-rebuilt.db"
            rebuilt = await rebuild(
                source, candidate, embedder.model_id, "offline://no-network"
            )
            assert rebuilt["cache_identity"] == "full_space"
            assert rebuilt["learned_turns"] == row["turns"]
            assert full_state(candidate) == expected, (
                "full learning or consumption state changed"
            )
            assert hashlib.sha256(source.read_bytes()).hexdigest() == before
            results.append(
                {
                    "question_id": identity,
                    "records": references,
                    "restart_exact": True,
                    "reader_inputs_unchanged": True,
                    "source_only_rebuild_equal": True,
                    "source_bytes_unchanged": True,
                    "learned_turns": rebuilt["learned_turns"],
                }
            )
            print(json.dumps({"question_id": identity, "verified": True}), flush=True)
    finally:
        socket.socket.connect, socket.socket.connect_ex = (
            original_connect,
            original_connect_ex,
        )
    assert attempts == 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "cases": len(results),
                "records": sum(len(r["records"]) for r in results),
                "network_attempts": attempts,
                "source_only": "rebuild receives sessions.db only, its temporary workspace contains no prior sidecar",
                "full_state": "all canonical learned tables plus complete kirakira_checkpoint JSON",
                "source_sha256": {
                    p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                    for p in [
                        "plugins/akasha2/engine.py",
                        "session/turn_source.py",
                        "session/manager.py",
                        "scripts/replay_akasha2.py",
                    ]
                },
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )


def main():
    p = argparse.ArgumentParser()
    for name in [
        "original-retrieval",
        "final-retrieval",
        "cases",
        "embeddings",
        "output",
    ]:
        p.add_argument("--" + name, type=Path, required=True)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
