"""Run production message consumption and retrieval on a fixed public dataset."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

os.environ["OPENBLAS_NUM_THREADS"] = "1"
import numpy as np

from agent.config_models import Config, MemoryConfig, MemoryEmbeddingConfig
from bus.events_lifecycle import TurnCommitted
from core.memory.engine import MemoryQuery
from core.net.http import SharedHttpResources
from plugins.akasha.config import AkashaConfig
from plugins.akasha.engine import AkashaMemoryEngine
from plugins.akasha2.domain.model import MemoryConfig as CycleConfig
from plugins.akasha2.engine import Akasha2MemoryEngine
from session.manager import SessionManager
from session.turn_source import load_committed_turn_messages

from .dataset import load_longmemeval
from .offline_ab import _turn_groups
from .protocol import chronological, select_fixed, timestamp


class CachedEmbedder:
    model_id = "BAAI/bge-small-en-v1.5"

    def __init__(self, cache):
        data = np.load(cache, allow_pickle=False)
        self.vectors = dict(zip(data["keys"].tolist(), data["vectors"], strict=True))

    async def embed(self, text):
        return self.vectors.get("q:" + text, self.vectors.get("p:" + text)).tolist()

    async def embed_batch(self, texts):
        return [self.vectors["p:" + text].tolist() for text in texts]

    def close(self):
        pass

    async def aclose(self):
        pass


def bounded(records, max_chars=24000, limit=10):
    text, kept = "", []
    for r in records[:limit]:
        block = r["summary"] + "\n"
        if len(text) + len(block) > max_chars:
            if not kept:
                text = block[:max_chars]
                kept.append(r)
            break
        text += block
        kept.append(r)
    return text, kept


async def prepare(a):
    cases = select_fixed(load_longmemeval(a.dataset), a.per_type, a.selection)
    embedder = CachedEmbedder(a.embeddings)
    expected_ids = json.loads(a.embeddings.with_suffix(".json").read_text())["case_ids"]
    if expected_ids != [c.question_id for c in cases]:
        raise ValueError("embedding selection differs from retrieval selection")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    completed = (
        {json.loads(line)["question_id"] for line in a.output.read_text().splitlines()}
        if a.output.exists()
        else set()
    )
    for case in cases[a.shard_index :: a.shards]:
        if case.question_id in completed:
            continue
        start = time.monotonic()
        path = a.workspace / case.question_id
        if path.exists():
            raise ValueError(
                "unfinished case workspace exists; use a fresh workspace or inspect it explicitly"
            )
        path.mkdir(parents=True)
        sessions = SessionManager(path)
        http = SharedHttpResources()
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
        v1 = AkashaMemoryEngine(
            config=config,
            akasha_config=AkashaConfig(),
            workspace=path,
            http_resources=http,
        )
        await v1._embedder.aclose()
        v1._embedder = embedder
        v2 = Akasha2MemoryEngine(
            config=config, workspace=path, http_resources=http, embedder=embedder
        )
        refs, turns = {}, []
        timings = {"ingest_seconds": {}, "query_seconds": {}}
        excluded_messages = 0
        index_time = max(
            [
                timestamp(case.question_date),
                *(timestamp(d) for d in case.haystack_dates),
            ]
        )
        for date, sid, messages in chronological(case):
            session = sessions.get_or_create("lme:" + sid)
            for group in _turn_groups(messages):
                pair = [
                    session.add_message(
                        t.role,
                        t.content,
                        timestamp=timestamp(date).isoformat(),
                        id=hashlib.sha256(
                            (
                                case.question_id
                                + ":"
                                + sid
                                + ":"
                                + str(len(session.messages))
                            ).encode()
                        ).hexdigest(),
                    )
                    for t in group
                ]
                sessions.save(session)
                if len(group) != 2 or [t.role for t in group] != ["user", "assistant"]:
                    excluded_messages += len(group)
                    continue
                if not load_committed_turn_messages(
                    path / "sessions.db",
                    TurnCommitted(
                        session_key=session.key,
                        channel="eval",
                        chat_id="",
                        assistant_message_id=pair[1]["id"],
                    ),
                ):
                    excluded_messages += 2
                    continue
                refs.update({p["id"]: sid for p in pair})
                turns.append(
                    {
                        "id": pair[0]["id"],
                        "source_session": sid,
                        "summary": f"[{date}] user: {group[0].content}\nassistant: {group[1].content}",
                        "texts": [t.content for t in group],
                    }
                )
        for name, engine in (("akasha", v1), ("akasha2", v2)):
            phase_started = time.perf_counter()
            while await engine.catch_up():
                pass
            timings["ingest_seconds"][name] = time.perf_counter() - phase_started
        v1._ensure_idf_table()
        query = await embedder.embed(case.question)
        dense = sorted(
            [
                {
                    "id": t["id"],
                    "summary": t["summary"],
                    "source_session": t["source_session"],
                    "score": max(
                        float(np.dot(query, embedder.vectors["p:" + text]))
                        for text in t["texts"]
                    ),
                }
                for t in turns
            ],
            key=lambda r: r["score"],
            reverse=True,
        )
        turn_by_id = {t["id"]: t for t in turns}
        arms = {"no_memory": {"context": "", "records": []}}
        for name in ("dense", "akasha", "akasha2"):
            trace = {}
            if name == "dense":
                records = dense
            else:
                e = v1 if name == "akasha" else v2
                samples = []
                first_records = None
                for _ in range(5):
                    phase_started = time.perf_counter()
                    result = await e.query(
                        MemoryQuery(
                            case.question,
                            limit=10,
                            effect="read_only",
                            timestamp=index_time,
                        )
                    )
                    samples.append(time.perf_counter() - phase_started)
                    if first_records is None:
                        first_records = result.records
                    elif result.records != first_records:
                        raise ValueError(
                            "repeated read-only query changed returned evidence"
                        )
                timings["query_seconds"][name] = samples
                records = [
                    {
                        "id": r.id,
                        "summary": turn_by_id[r.evidence[0].refs[0]]["summary"],
                        "score": r.score,
                        "source_session": refs[r.evidence[0].refs[0]],
                        "signals": r.signals,
                    }
                    for r in result.records
                ]
                trace = result.trace
            context, kept = bounded(records)
            gold = set(case.answer_session_ids)
            found = {r["source_session"] for r in kept}
            arms[name] = {
                "context": context,
                "records": kept,
                "trace": trace,
                "evidence_session_recall": len(found & gold) / len(gold)
                if gold
                else None,
                "all_evidence_sessions": gold <= found if gold else None,
            }
        await v2.aclose()
        for closer in reversed(v1.closeables):
            if hasattr(closer, "aclose"):
                await closer.aclose()
            elif hasattr(closer, "close"):
                closer.close()
        sessions.close()
        row = {
            "question_id": case.question_id,
            "question_type": case.question_type,
            "question": case.question,
            "question_date": case.question_date,
            "answer": case.answer,
            "abstention": case.question_id.endswith("_abs"),
            "answer_session_ids": case.answer_session_ids,
            "sessions": len(case.haystack_sessions),
            "turns": len(turns),
            "unpaired_messages_excluded_from_all_arms": excluded_messages,
            "index_time": index_time.isoformat(),
            "seconds": time.monotonic() - start,
            "adapter_timings": timings,
            "arms": arms,
        }
        with a.output.open("a") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
        print(
            json.dumps(
                {
                    "question_id": case.question_id,
                    "turns": len(turns),
                    "unpaired_messages_excluded_from_all_arms": excluded_messages,
                    "index_time": index_time.isoformat(),
                    "seconds": row["seconds"],
                }
            ),
            flush=True,
        )
    a.output.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "shards": a.shards,
                "shard_index": a.shard_index,
                "engine_config": {
                    "akasha": asdict(AkashaConfig()),
                    "akasha2": asdict(CycleConfig()),
                },
                "openblas_threads": 1,
                "dataset_sha256": hashlib.sha256(a.dataset.read_bytes()).hexdigest(),
                "dataset_repository": "xiaowu0162/longmemeval-cleaned",
                "dataset_revision": "98d7416c24c778c2fee6e6f3006e7a073259d48f",
                "source_hashes": {
                    str(f): hashlib.sha256(f.read_bytes()).hexdigest()
                    for root in (
                        "agent",
                        "bootstrap",
                        "bus",
                        "core",
                        "infra",
                        "plugins",
                        "session",
                        "eval",
                    )
                    for f in Path(root).rglob("*.py")
                },
                "selection": json.loads(a.selection.read_text())
                if a.selection
                else "sha256(kirakira-20261019:question_id), first per type, no label selection",
                "case_ids": [c.question_id for c in cases],
                "context_chars": 24000,
                "max_turns": 10,
                "readout": "all retrieval arms resolve full original message pairs with identical date/text formatting; no engine-specific assistant clipping",
                "timestamps": "UTC consistently assumed; sessions sorted by date; all supplied haystacks consumed at max(question date, history dates); reader reasons relative to original question date",
                "projection": "all raw messages persisted; graph and dense arms retrieve only complete user/assistant pairs; unpaired count reported per case",
                "embedding_manifest": json.loads(
                    a.embeddings.with_suffix(".json").read_text()
                ),
            },
            indent=2,
        )
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--embeddings", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--per-type", type=int, default=6)
    p.add_argument("--selection", type=Path)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--shard-index", type=int, default=0)
    asyncio.run(prepare(p.parse_args()))


if __name__ == "__main__":
    main()
