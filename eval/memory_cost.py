"""Measure what the memory engine costs to learn and to query.

Deterministic and model-free: fixed vectors, real learning and real retrieval
through the production components. Reports learn time, retrieval time and peak
allocation separately, because a change can trade one for another.

This bounds cost only. It says nothing about retrieval quality, so a faster or
leaner run is not evidence of an equally useful one; `eval/memory_depth.py` and
`eval/memory_runtime.py` are where answer quality is measured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import tracemalloc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from agent.plugin_composition import CompositionRoot, Context
from plugins.akasha.application.consumer import MessageConsumer
from plugins.akasha.contract import ContextSource
from plugins.akasha.domain.model import MemoryConfig
from plugins.akasha.learning import AKASHA_LEARNING, Learning, LearningConfig
from plugins.akasha.recalls import query_memory
from plugins.content.api import legacy_post_commit_effect
from plugins.content.plugin import check_text
from plugins.ledger.bindings import Bindings
from plugins.ledger.contract import ContentPart, Input, Output
from plugins.ledger.embedding_store import MessageEmbeddings
from plugins.ledger.log import MessageLog, SessionAttributes
from plugins.turn_projection.plugin import TurnProjection

SESSION = "conversation"
SOURCE = "conversation"
DIMENSION = 16


def _vector(index: int) -> list[float]:
    """A stable pseudo-random unit vector, so runs are comparable."""
    generator = np.random.default_rng(index)
    raw = generator.normal(size=DIMENSION)
    return (raw / np.linalg.norm(raw)).tolist()


@asynccontextmanager
async def _memory(path: Path) -> AsyncIterator[tuple[MessageConsumer, Learning, MessageLog, MessageEmbeddings, Bindings]]:
    log = MessageLog(path / "sessions.db")
    log.ensure_session(SESSION, SessionAttributes())
    embeddings = MessageEmbeddings(log, log._path.with_name("sessions-derived.db"))
    root = CompositionRoot("memory-cost")
    learning = Learning(TurnProjection(), owner="akasha", post_commit_effect=legacy_post_commit_effect)
    bindings = Bindings(log, root.context)
    rule = LearningConfig(embedding_model="fixed", dimension=DIMENSION, sources=(SOURCE,))
    log.save_binding("fixed-learning", {"version": 1, "service": AKASHA_LEARNING.name,
                                        "root_ref": "memory-cost", "metadata": rule.model_dump()})

    async def provide(ctx: Context) -> None:
        await ctx.provide(AKASHA_LEARNING, learning)

    async def embed(texts: list[str]) -> list[list[float]]:
        return [_vector(abs(hash(text)) % 100_000) for text in texts]

    await root.mount(provide, name="learning")
    if not root.receipt().ready:
        raise RuntimeError(repr(root.receipt()))
    consumer = await MessageConsumer.load(path / "memory.db", catalog=log.catalog(),
                                          embeddings=embeddings, bindings=bindings,
                                          config=MemoryConfig(), cutover=False)
    try:
        yield consumer, learning, log, embeddings, bindings
    finally:
        consumer.close()
        await root.dispose()
        embeddings.close()
        log.close()


async def measure(*, turns: int, queries: int) -> dict[str, object]:
    with TemporaryDirectory(prefix="memory-cost-") as directory:
        path = Path(directory)
        async with _memory(path) as (consumer, learning, log, embeddings, bindings):
            vectors = embeddings.bind(learning.text)
            for index in range(turns):
                for author, kind, body in (
                    ("user", Input, Input((ContentPart("text", f"第 {index} 轮：预算、场地与运输安排的讨论"),))),
                    ("assistant", Output, Output((ContentPart("text", f"第 {index} 轮已记录"),), "complete")),
                ):
                    message = log.writer(SESSION, author=author, source=SOURCE, body_types=(kind,),
                                         content={"text": check_text}).append(f"{author}-{index}", body)
                    vectors.save(message, model="fixed", embedding=_vector(index * 2 + (author == "assistant")))

            tracemalloc.start()
            start = time.perf_counter()
            learned = await consumer.consume(
                catalog=log.catalog(), learning_binding="fixed-learning",
                embeddings=embeddings, bindings=bindings,
                embed_batch=lambda texts: _embed_batch(texts),
            )
            learn_seconds = time.perf_counter() - start
            _, learn_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            tracemalloc.start()
            latencies: list[float] = []
            stamp = datetime.now(UTC)
            for index in range(queries):
                begin = time.perf_counter()
                _ = query_memory(
                    consumer.cycle, consumer.state, learning_binding="fixed-learning",
                    text=f"预算和运输的安排 {index}", dense=np.array(_vector(10_000 + index)),
                    stamp=stamp,
                    source=ContextSource(session_id=SESSION, source=SOURCE,
                                         through_seq=log.reader(SESSION).snapshot()[-1].seq),
                    limit=40, selection="fused",
                )
                latencies.append(time.perf_counter() - begin)
            _, query_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            return {
                "conditions": {"turns": turns, "queries": queries, "dimension": DIMENSION,
                               "chat_model": None, "embedding": "fixed pseudo-random unit vectors"},
                "learn": {"turns_learned": learned,
                          "seconds": round(learn_seconds, 4),
                          "peak_mib": round(learn_peak / 1048576, 2)},
                "query": {"count": len(latencies),
                          "mean_ms": round(statistics.mean(latencies) * 1000, 3),
                          "median_ms": round(statistics.median(latencies) * 1000, 3),
                          "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1] * 1000, 3),
                          "total_seconds": round(sum(latencies), 4),
                          "peak_mib": round(query_peak / 1048576, 2)},
                "caveat": "只衡量开销，不衡量检索质量；更快或更省不等于答案同样有用。",
            }


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    return [_vector(abs(hash(text)) % 100_000) for text in texts]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=400)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = asyncio.run(measure(turns=args.turns, queries=args.queries))
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
