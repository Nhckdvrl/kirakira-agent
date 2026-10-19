"""Kirakira's publication adapter for the pinned Akasha2 pure memory cycle.

Original messages own truth. One atomic SQLite checkpoint owns graph, consumed
position, message bindings and embedding space. Queries never commit learning.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from bus.events_lifecycle import TurnCommitted
from core.memory.engine import (
    EngineProfile,
    EvidenceRef,
    MemoryCapability,
    MemoryEngineDescriptor,
    MemoryIngestResult,
    MemoryMutationResult,
    MemoryQueryResult,
    MemoryRecord,
    MemoryToolProfile,
    MemoryToolSpec,
)
from memory2.embedder import Embedder
from plugins.akasha.consumption import publications
from plugins.ledger.embedding_store import MessageEmbeddingStore
from plugins.ledger.turn_source import (
    committed_turn_from_snapshot,
    original_message_snapshot,
)

from .application.cycle import MemoryCycle
from .domain.model import ContextState, MemoryConfig, Turn
from .infrastructure.lexical import tokenize
from .infrastructure.persistence import load_memory_state, write_memory_database
from .lease import CheckpointLease
from .selection import select_hits


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    assistant_id: str
    source_digest: str
    user_dense: list[float]
    assistant_dense: list[float]


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: int = Field(default=1, ge=1, le=1)
    stream: str
    cursor: int = Field(ge=0)
    embedding_space: str
    bindings: list[Binding]


def _digest(messages):
    return hashlib.sha256(
        json.dumps(
            [asdict(m) for m in messages],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("memory timestamps must include timezone")
    return parsed


def _vector(value, *, normalized=False):
    vector = np.asarray(value, dtype=np.float64)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        raise ValueError("embedding must be a finite nonempty vector")
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("embedding must have a finite positive norm")
    if normalized:
        if not np.isclose(norm, 1.0, rtol=1e-12, atol=1e-12):
            raise ValueError("checkpoint binding must retain a normalized embedding")
        return vector
    return vector / norm


class Akasha2MemoryEngine:
    DESCRIPTOR = MemoryEngineDescriptor(
        name="akasha2",
        profile=EngineProfile.RICH_MEMORY_ENGINE,
        capabilities=frozenset(
            {
                MemoryCapability.INGEST_MESSAGES,
                MemoryCapability.RETRIEVE_SEMANTIC,
                MemoryCapability.RETRIEVE_CONTEXT_BLOCK,
                MemoryCapability.RETRIEVE_STRUCTURED_HITS,
                MemoryCapability.SEMANTICS_RICH_MEMORY,
            }
        ),
        notes={
            "owner": "plugins.akasha2",
            "truth": "sessions.db/messages",
            "upstream": "d85b00c",
            "checkpoint_contract": "kirakira.akasha2.v1",
        },
    )

    def __init__(
        self,
        *,
        config,
        workspace: Path,
        http_resources,
        event_publisher=None,
        embedder=None,
        rebuild_from_start=False,
        checkpoint_every=16,
    ):
        if checkpoint_every < 1:
            raise ValueError("checkpoint interval must be positive")
        self._source = workspace / "sessions.db"
        self._path = workspace / "memory" / "akasha2.db"
        self._cfg = MemoryConfig()
        self._lock = asyncio.Lock()
        self._poisoned = False
        self._closed = False
        self._dirty = 0
        self._fixed_vectors = None
        self._every = checkpoint_every
        embedding = config.memory.embedding
        self._dimensions = embedding.output_dimensionality
        self._embedder = embedder or Embedder(
            base_url=embedding.base_url or "",
            api_key=embedding.api_key,
            model=embedding.model,
            output_dimensionality=embedding.output_dimensionality,
            requester=http_resources.external_default,
        )
        self._space = json.dumps(
            {
                "model": self._embedder.model_id,
                "dimensions": embedding.output_dimensionality,
                "endpoint": embedding.base_url,
            },
            sort_keys=True,
        )
        self._lease = CheckpointLease(self._path.with_suffix(".writer.lock"))
        try:
            stream, head = self._source_head()
            if rebuild_from_start and self._path.exists():
                raise ValueError(
                    "rebuild requires a separate workspace; existing checkpoint is never overwritten"
                )
            self._checkpoint = Checkpoint(
                stream=stream,
                cursor=0 if rebuild_from_start else head,
                embedding_space=self._space,
                bindings=[],
            )
            self._cycle = MemoryCycle(self._cfg)
            if self._path.exists():
                self._restore(stream, head)
            else:
                self._publish()
            self._unsubscribe = (
                event_publisher.on(TurnCommitted, self._on_turn)
                if event_publisher
                else None
            )
            # The engine closes its subscription, publishes its tail, then its embedder.
            self.closeables = [self]

        except BaseException:
            self._lease.close()
            close = getattr(self._embedder, "close", None)
            if close:
                close()
            raise

    @property
    def embedding_api(self):
        return self._embedder

    def _source_head(self):
        with closing(sqlite3.connect(self._source)) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_publications'"
            ).fetchone()
            if not exists:
                raise ValueError("initialize SessionManager before Akasha2")
            stream = db.execute(
                "SELECT stream_id FROM message_publication_stream WHERE singleton=1"
            ).fetchone()[0]
            head = db.execute(
                "SELECT coalesce(max(position),0) FROM message_publications"
            ).fetchone()[0]
        return stream, head

    def _messages(self, assistant_id, source=None):
        if source is None:
            with original_message_snapshot(self._source) as source:
                return self._messages(assistant_id, source)
        row = source.execute(
            "SELECT session_key FROM messages WHERE id=?", (assistant_id,)
        ).fetchone()
        if row is None:
            raise ValueError(
                "checkpoint source message is missing; explicit rebuild required"
            )
        return committed_turn_from_snapshot(
            source,
            TurnCommitted(
                session_key=row[0],
                channel="memory",
                chat_id="",
                assistant_message_id=assistant_id,
            ),
        )

    def _turn(
        self,
        messages,
        vectors,
        *,
        query=False,
        text="",
        timestamp=None,
        normalized=False,
    ):
        previous = self._cycle.turns[-1] if self._cycle.turns else None
        if query:
            when = timestamp.isoformat()
            uid, aid, session, seq, user, assistant = (
                "query",
                "",
                "__query__",
                0,
                text,
                "",
            )
        else:
            u, a = messages
            uid, aid, session, seq, user, assistant = (
                u.id,
                a.id,
                u.session_key,
                u.seq,
                u.content,
                a.content,
            )
            when = u.ts
        gap = (
            None
            if previous is None
            else max(
                0.0,
                (_timestamp(when) - _timestamp(previous.started_at)).total_seconds(),
            )
        )
        return Turn(
            node_id=len(self._cycle.turns),
            turn_id=uid,
            session_key=session,
            user_seq=seq,
            user_message_id=uid,
            assistant_message_id=aid,
            started_at=when,
            committed_at=when if query else messages[1].ts,
            user_text=user,
            assistant_text=assistant,
            user_dense=_vector(vectors[0], normalized=normalized),
            assistant_dense=None
            if query
            else _vector(vectors[1], normalized=normalized),
            user_terms=tuple(sorted(tokenize(user).items())),
            assistant_terms=tuple(sorted(tokenize(assistant).items())),
            inter_gap_seconds=gap,
        )

    def _restore(self, stream, head):
        with closing(
            sqlite3.connect(f"{self._path.resolve().as_uri()}?mode=ro", uri=True)
        ) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key='kirakira_checkpoint'"
            ).fetchone()
        if row is None:
            raise ValueError("Akasha2 checkpoint has no consumption contract")
        checkpoint = Checkpoint.model_validate_json(row[0])
        if (
            checkpoint.stream != stream
            or checkpoint.cursor > head
            or checkpoint.embedding_space != self._space
        ):
            raise ValueError(
                "memory source or embedding space differs; explicit rebuild required"
            )
        turns = []
        previous_position = 0
        with original_message_snapshot(self._source) as source:
            for binding in checkpoint.bindings:
                position = source.execute(
                    "SELECT position FROM message_publications WHERE message_id=?",
                    (binding.assistant_id,),
                ).fetchone()
                if (
                    position is None
                    or not previous_position < position[0] <= checkpoint.cursor
                ):
                    raise ValueError(
                        "checkpoint consumption order differs from source publications"
                    )
                previous_position = position[0]
                messages = self._messages(binding.assistant_id, source)
                if not messages or _digest(messages) != binding.source_digest:
                    raise ValueError(
                        "original messages changed since learning; explicit rebuild required"
                    )
                # _turn uses the exact preceding original turn for the causal gap.
                self._cycle.turns = turns
                turns.append(
                    self._turn(
                        messages,
                        [binding.user_dense, binding.assistant_dense],
                        normalized=True,
                    )
                )
        graph, events, evidence, context, recalls, bursts = load_memory_state(
            self._path, turns=turns, config=self._cfg
        )
        self._cycle = MemoryCycle.restore(
            config=self._cfg,
            turns=turns,
            graph=graph,
            events=events,
            evidence=evidence,
            context=context,
            recalls=recalls,
            burst_members=bursts,
        )
        self._checkpoint = checkpoint

    def _check(self):
        if self._poisoned:
            raise RuntimeError(
                "memory checkpoint publication failed; restart to recover the published state"
            )
        if self._closed:
            raise RuntimeError("memory engine is closed")

    def _publish(self):
        try:
            c = self._cycle
            write_memory_database(
                self._path,
                turns=c.turns,
                graph=c.graph,
                events=c.events,
                evidence=c.evidence,
                captures=[],
                context=c.context or ContextState((), None, ()),
                burst_members=c.burst_members,
                config=self._cfg,
                recalls=c.recalls,
                metadata={"kirakira_checkpoint": self._checkpoint.model_dump_json()},
            )
            self._dirty = 0
        except BaseException:
            self._poisoned = True
            raise

    async def _worker(self, function, *args):
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        cancellation = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                cancellation = exc
        result = worker.result()
        if cancellation is not None:
            raise cancellation
        return result

    async def _fixed_pair(self, messages):
        # Raw float32 vectors are durable replay inputs; normalized bindings alone
        # cannot recreate the original provider values after a sidecar is lost.
        if self._fixed_vectors is None:
            self._fixed_vectors = MessageEmbeddingStore(self._source)
        vectors = [
            self._fixed_vectors.get(
                message_id=m.id, content=m.content, model=self._space
            )
            for m in messages
        ]
        missing = [i for i, vector in enumerate(vectors) if vector is None]
        if missing:
            fresh = await self._embedder.embed_batch(
                [messages[i].content for i in missing]
            )
            if len(fresh) != len(missing):
                raise ValueError("embedding count differs from original message pair")
            for index, value in zip(missing, fresh, strict=True):
                vectors[index] = value
        if self._messages(messages[-1].id) != messages:
            raise ValueError("memory source changed during embedding")
        with np.errstate(over="ignore", invalid="ignore"):
            canonical = [np.asarray(vector, dtype=np.float32) for vector in vectors]
        for vector in canonical:
            _vector(vector)
            if self._dimensions is not None and vector.shape != (self._dimensions,):
                raise ValueError(
                    "embedding dimensions disagree with the declared space"
                )
        expected = (
            self._cycle.turns[0].user_dense.shape
            if self._cycle.turns
            else canonical[0].shape
        )
        if any(vector.shape != expected for vector in canonical):
            raise ValueError("embedding dimensions changed")
        # Each successful write survives a failed learning/publish attempt. Source
        # messages and publication order are untouched by this separate cache.
        for index in missing:
            message = messages[index]
            self._fixed_vectors.upsert(
                message_id=message.id,
                content=message.content,
                model=self._space,
                embedding=canonical[index].tolist(),
            )
        return [vector.tolist() for vector in canonical]

    async def catch_up(self, *, max_items=64):
        async with self._lock:
            self._check()
            stream, head = self._source_head()
            if stream != self._checkpoint.stream or head < self._checkpoint.cursor:
                raise ValueError("memory source stream changed")
            pending = publications(self._source, self._checkpoint.cursor, max_items)
            for publication in pending:
                if publication["extra_json"] is None:
                    raise ValueError("unconsumed original message was deleted")
                messages = (
                    []
                    if publication["session_key"].startswith("scheduler:")
                    else self._messages(publication["message_id"])
                )
                try:
                    if messages:
                        vectors = await self._fixed_pair(messages)
                        turn = self._turn(messages, vectors)
                        if (
                            self._cycle.turns
                            and turn.user_dense.shape
                            != self._cycle.turns[0].user_dense.shape
                        ):
                            raise ValueError("embedding dimensions changed")
                        await self._worker(self._cycle.commit, turn, None)
                        self._checkpoint = self._checkpoint.model_copy(
                            update={
                                "bindings": [
                                    *self._checkpoint.bindings,
                                    Binding(
                                        assistant_id=messages[1].id,
                                        source_digest=_digest(messages),
                                        user_dense=turn.user_dense.tolist(),
                                        assistant_dense=turn.assistant_dense.tolist(),
                                    ),
                                ]
                            }
                        )
                    self._checkpoint = self._checkpoint.model_copy(
                        update={"cursor": publication["position"]}
                    )
                    self._dirty += 1
                    if self._dirty >= self._every:
                        await self._worker(self._publish)
                except BaseException:
                    # Learning may have partially mutated arrays. Never use it again.
                    self._poisoned = True
                    raise
            return len(pending)

    async def _on_turn(self, event):
        await self.catch_up()

    async def run_maintenance(self):
        while True:
            await self.catch_up()
            await asyncio.sleep(30)

    async def query(self, request):
        if request.intent == "timeline":
            return MemoryQueryResult(
                trace={"engine": "akasha2", "intent": "timeline_unsupported"}
            )
        if not request.text.strip():
            return MemoryQueryResult(trace={"engine": "akasha2", "hit_count": 0})
        if request.timestamp is None or request.timestamp.tzinfo is None:
            raise ValueError(
                "memory query requires an explicit timezone-aware timestamp"
            )
        async with self._lock:
            self._check()
            if not self._cycle.turns:
                return MemoryQueryResult(trace={"engine": "akasha2", "hit_count": 0})
            # Reject historical views rather than retrieve from a future learned graph.
            if any(
                _timestamp(t.committed_at) > request.timestamp
                for t in self._cycle.turns
            ):
                raise ValueError(
                    "historical query requires a checkpoint before its cutoff"
                )
            vec = await self._embedder.embed(request.text)
            if _vector(vec).shape != self._cycle.turns[0].user_dense.shape:
                raise ValueError("query embedding space changed")
            cue = self._turn(
                [],
                [vec],
                query=True,
                text=request.text,
                timestamp=request.timestamp,
            )
            ticket = await self._worker(self._cycle.retrieve, cue)
            records = []
            limit = max(0, min(20, int(request.limit)))
            hits = select_hits(
                self._cycle.turns,
                ticket,
                cue,
                inhibited=self._cycle.inhibited_nodes,
                limit=0
                if request.filters.kinds and "turn" not in request.filters.kinds
                else limit,
                strong=request.filters.relevance_floor == "strong",
                time_start=request.filters.time_start,
                time_end=request.filters.time_end,
            )
            with original_message_snapshot(self._source) as source:
                for item in hits:
                    turn = self._cycle.turns[item.node_id]
                    messages = self._messages(turn.assistant_message_id, source)
                    if (
                        _digest(messages)
                        != self._checkpoint.bindings[item.node_id].source_digest
                    ):
                        raise ValueError(
                            "retrieved original messages changed; rebuild required"
                        )
                    refs = [turn.user_message_id, turn.assistant_message_id]
                    records.append(
                        MemoryRecord(
                            id=turn.turn_id,
                            kind="turn",
                            summary=f"[{turn.started_at}] user: {messages[0].content}\nassistant: {messages[1].content}",
                            score=item.score,
                            engine_kind="akasha2",
                            evidence=[
                                EvidenceRef(
                                    kind="message_range",
                                    refs=refs,
                                    source_ref=json.dumps(refs),
                                )
                            ],
                            signals={
                                "lane": item.lane,
                                "sources": list(item.sources),
                                "basin_ids": list(item.basin_ids),
                            },
                        )
                    )
            return MemoryQueryResult(
                records=records,
                text_block="\n\n".join(
                    f"{r.summary}\nsource_ref={r.evidence[0].source_ref}"
                    for r in records
                ),
                trace={
                    "engine": "akasha2",
                    "readout": "dense5+completion, total record limit",
                    "state_version": self._cycle.state_version,
                    "cursor": self._checkpoint.cursor,
                    "checkpoint_pending": self._dirty,
                    "source_snapshot": True,
                    "verified_messages": 2 * len(records),
                    "pushes": ticket.completion.pushes,
                    "residual_l1": ticket.completion.residual_l1,
                },
                raw={
                    "items": [
                        {
                            "id": r.id,
                            "summary": r.summary,
                            "score": r.score,
                            "source_ref": r.evidence[0].source_ref,
                        }
                        for r in records
                    ]
                },
            )

    async def ingest(self, request):
        return MemoryIngestResult(
            accepted=False, raw={"reason": "original committed messages only"}
        )

    async def mutate(self, request):
        return MemoryMutationResult(accepted=False, status="unsupported")

    def reinforce_items_batch(self, ids):
        if ids:
            raise NotImplementedError(
                "Akasha2 feedback must be bound to a committed source turn"
            )

    def describe(self):
        return self.DESCRIPTOR

    def tool_profile(self):
        return MemoryToolProfile(
            recall=MemoryToolSpec(
                description="召回跨会话原始对话，返回可回源的 source_ref。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                },
            )
        )

    async def aclose(self):
        async with self._lock:
            if self._closed:
                return
            try:
                if self._dirty and not self._poisoned:
                    self._publish()
            finally:
                self._closed = True
                if self._fixed_vectors is not None:
                    self._fixed_vectors.close()
                self._lease.close()
                if self._unsubscribe:
                    self._unsubscribe()
                close = getattr(self._embedder, "aclose", None)
                if close:
                    await close()
                elif hasattr(self._embedder, "close"):
                    self._embedder.close()
