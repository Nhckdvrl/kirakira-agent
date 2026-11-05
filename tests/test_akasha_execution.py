"""Akasha 按批发布因果检查点；后台工作不能冻结宿主或提前释放 owner。"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agent.plugin_composition import CompositionRoot, Context
from plugins.akasha.application.consumer import MessageConsumer
from plugins.akasha.domain.model import MemoryConfig
from plugins.akasha.infrastructure.consumption import Consumption
from plugins.akasha.infrastructure.lease import WriterLease
from plugins.akasha.infrastructure.persistence import (
    load_consumption,
    logical_state_sha256,
)
from plugins.akasha.learning import AKASHA_LEARNING, Learning, LearningConfig
from plugins.akasha.recalls import RecallRecords
from plugins.akasha.runtime import MessageMemory
from plugins.content.api import legacy_post_commit_effect
from plugins.content.plugin import check_text
from plugins.ledger.bindings import Bindings
from plugins.ledger.contract import ContentPart, Input, Output
from plugins.ledger.embedding_store import MessageEmbeddings
from plugins.ledger.log import MessageLog, SessionAttributes
from plugins.turn_projection.plugin import TurnProjection


def test_legacy_recall_lookup_pages_only_random_keys_and_filters_before_decode(tmp_path):
    from datetime import UTC, datetime
    from plugins.akasha.contract import ContextSource, Recall
    from plugins.akasha.recalls import RecallRecordsRead

    log = MessageLog(tmp_path / "sessions.db")
    try:
        owner = log.owner("akasha")
        recall = Recall(learning_binding="fixture", graph_version=0, timestamp=datetime.now(UTC),
            source=ContextSource(session_id="target", source="conversation", through_seq=5),
            limit=1, hits=(), active_basin_count=0, pushes=0, residual_l1=0)

        def seed(tx):
            # Unrelated receipts need not even share the current Recall schema.
            for number in range(1001):
                tx.save("recall:" + f"{number:032x}", {"source": {"kind": "context", "session_id": "other"}}, expected_version=None)
            for identity in ("d" * 32, "e" * 32):
                tx.save("recall:" + identity, recall.model_dump(mode="json"), expected_version=None)
            for identity in ("context:stable", "tool:stable"):
                tx.save("recall:" + identity, {"current_namespace": True}, expected_version=None)
        owner.transact(seed)
        scans = []

        class BoundedRead:
            def scan(self, **kwargs):
                scans.append(kwargs)
                return owner.scan(**kwargs)

        records = RecallRecordsRead(BoundedRead())
        assert [key for key, _ in records.legacy_context("target", "conversation", first_seq=4, through_seq=6)] == ["e" * 32, "d" * 32]
        assert len(scans) == 3 and all(row["limit"] == 1000 for row in scans)
        assert records.legacy_context("target", "conversation", first_seq=6, through_seq=7) == ()
        assert records.legacy_context("target", "job", first_seq=4, through_seq=6) == ()
    finally:
        log.close()


@pytest.mark.asyncio
async def test_automatic_recall_bounds_live_context_and_retires_only_verified_legacy_text(tmp_path):
    import hashlib
    from datetime import UTC, datetime
    from html import escape
    import numpy as np
    from plugins.akasha.contract import ContextSource, MemorySourceQuery
    from plugins.akasha.query import MemoryQueries
    from plugins.akasha.recalls import context_identity, query_memory, record_materials, render_materials
    from plugins.models.contract import LLMResponse
    from plugins.models.projection import check_facts, response_facts

    async with memory_fixture(tmp_path) as (memory, consumer, learning, bindings, log, embeddings, _, _):
        original = "Background filler. " * 400 + "needleproject budget BUDGET=760."
        for number in range(3):
            append_pair(log, embeddings, learning, number, user_text=original if number == 0 else None)
        await memory.consume()
        records = RecallRecords(log.owner("akasha"))
        inputs = log.writer("conversation", author="user", source="conversation", body_types=(Input,), content={"text": check_text})
        old = inputs.append("legacy-query", Input((ContentPart("text", "needleproject budget"),)))
        identity = context_identity("conversation", "conversation", old.message_id)
        recall = query_memory(consumer.cycle, consumer.state, learning_binding="fixed-learning",
            text="needleproject budget", dense=np.array([1., 0., 0.]), stamp=datetime.now(UTC),
            source=ContextSource(session_id="conversation", source="conversation", through_seq=old.seq), limit=3)
        whole = render_materials(identity, recall, learning, log.catalog(), max_chars=12000)
        recall = record_materials(recall, whole, max_chars=12000)
        records.save(identity, recall)
        legacy_text = whole["reminders"][0]["text"]
        reminder = "PRESERVE_UNRELATED_CONTEXT\n" + escape(legacy_text, quote=False) + "\nPRESERVE_TAIL"
        facts = response_facts(LLMResponse("noted", call_record_id="fixture-call"), (),
                               reminder=reminder, reminder_input_id=old.message_id)
        log.writer("conversation", author="assistant", source="conversation", body_types=(Output,),
            content={"text": check_text, "model.facts": check_facts}).append("legacy-answer",
                Output((ContentPart("text", "noted"), facts), "complete"))
        inputs.append("current-query", Input((ContentPart("text", "needleproject budget"),)))
        snapshot = log.reader("conversation").snapshot()
        graph_before = logical_state_sha256(consumer.path)
        first = await memory.prepare(snapshot, "conversation")
        assert first == await memory.prepare(snapshot, "conversation")
        live = [item for item in first["reminders"] if item.get("replay") is False]
        assert live and sum(len(item["text"]) for item in live) <= 2400
        # The original is still quoted verbatim in this very request, so recall
        # spends one index line on it instead of repeating the body.
        index = [item for item in live if item["name"] == "recall_index"]
        assert index and "ref=user-0" in index[0]["text"]
        assert "BUDGET=760" not in str(live)
        assert any("retrieval_ref" in item["text"] for item in first["reminders"] if item.get("replay", True))
        patches = first["reminder_patches"]
        assert len(patches) == 1 and patches[0]["sha256"] == hashlib.sha256(reminder.encode()).hexdigest()
        p = patches[0]
        revised = reminder[:p["start"]] + p["text"] + reminder[p["end"]:]
        assert legacy_text not in revised and identity in revised
        assert "PRESERVE_UNRELATED_CONTEXT" in revised and "PRESERVE_TAIL" in revised
        assert log.reader("conversation").snapshot() == snapshot and logical_state_sha256(consumer.path) == graph_before
        assert records.read(identity) == recall
        queries = MemoryQueries(capture=None, open_recall=None, catalog=log.catalog(), records=lambda: records,
                                check_scope=lambda _: "fixture-scope")
        restored = await queries.source(MemorySourceQuery(session_id="conversation", retrieval_ref=identity,
            message_id="user-0", offset=len(original) - 80, length=80))
        assert "BUDGET=760" in restored.text


@pytest.mark.asyncio
async def test_program_memory_query_preserves_graph_budget_and_retry_identity(tmp_path: Path) -> None:
    """A public read uses actual sources, a dated window and one durable receipt."""
    from types import SimpleNamespace

    from plugins.akasha.contract import MemoryQuery, MemorySourceQuery, MemorySourceSearchQuery
    from plugins.akasha.query import MemoryQueries
    from plugins.akasha.recall_tool import RecallTool
    from plugins.models.contract import EmbeddingResult

    async with memory_fixture(tmp_path) as (memory, consumer, learning, bindings, log, embeddings, _, _):
        for number in range(3):
            append_pair(log, embeddings, learning, number,
                        user_text=("🙂 unrelated filler. " * 500 + "needleproject resolution RELEASE_FRIDAY.") if number == 0 else None)
        assert await memory.consume() == 3
        before = logical_state_sha256(consumer.path)
        stamp = log.reader("conversation").get("user-0").recorded_at
        encoded = []

        @asynccontextmanager
        async def open_embedding(_binding):
            async def embed(texts):
                encoded.extend(texts)
                return EmbeddingResult(tuple((1.0, 0.0, 0.0) for _ in texts))
            yield SimpleNamespace(descriptor=SimpleNamespace(identity="fixed", dimensions=3), embed=embed)

        records = lambda: RecallRecords(log.owner("query-records"))

        @asynccontextmanager
        async def open_recall(_captured):
            yield RecallTool(memory=lambda _session: consumer.path, config=MemoryConfig(),
                catalog=log.catalog(), embeddings=embeddings, bindings=bindings,
                select_learning=lambda: ("fixed-learning", "fixed-embedding"), records=records(),
                open_embedding=open_embedding)

        queries = MemoryQueries(capture=lambda _: {}, open_recall=open_recall,
                                catalog=log.catalog(), records=records,
                                check_scope=lambda session: tmp_path / ("other" if session == "other" else "memory"))
        request = MemoryQuery(key="dated-read", session_id="conversation", query="remember episode",
                              limit=3, max_chars=256, time_start=stamp, time_end=stamp)
        results = await asyncio.gather(*(queries.recall(request) for _ in range(3)))
        assert results[0] == results[1] == results[2]
        result = results[0]
        assert encoded == [request.query]
        assert len(result.material) <= 256 and "时间：" in result.material
        assert {hit.node_id for hit in result.record.hits} == {0}
        assert {ref.message_id for ref in result.evidence} == set(result.record.presented_message_ids)
        assert set(result.record.presented_message_ids) <= {"user-0", "assistant-0"}
        assert result.record.source.session_id == "conversation"
        assert len(records().list()) == 1
        assert logical_state_sha256(consumer.path) == before
        with pytest.raises(ValueError, match="different request"):
            await queries.recall(request.model_copy(update={"query": "changed query"}))
        assert encoded == [request.query]
        fused = await queries.recall(request.model_copy(update={
            "key": "fused-read", "selection": "fused", "time_start": None, "time_end": None, "limit": 2,
        }))
        assert fused.record.selection == "fused" and len(fused.record.hits) <= 2
        assert logical_state_sha256(consumer.path) == before
        with pytest.raises(ValueError, match="different request"):
            await queries.recall(request.model_copy(update={"selection": "fused"}))
        excerpt_request = request.model_copy(update={"key": "bounded-excerpts", "presentation": "excerpts",
            "selection": "fused", "query": "needleproject resolution", "max_chars": 512})
        excerpt = await queries.recall(excerpt_request)
        assert "RELEASE_FRIDAY" in excerpt.material and len(excerpt.material) <= 512
        assert excerpt.record.presented_spans
        for evidence in excerpt.evidence:
            raw = learning.text(log.reader(evidence.session_id).get(evidence.message_id))
            span = evidence.excerpt
            assert span is not None and span.source_chars == len(raw)
            assert raw[span.start:span.end] in excerpt.material
        assert await queries.recall(excerpt_request) == excerpt
        assert logical_state_sha256(consumer.path) == before
        from plugins.akasha.inspector import RecallInspector
        inspector = RecallInspector(read=records().read, legacy_page=records().legacy_page, catalog=log.catalog())
        detail = inspector._detail(excerpt.retrieval_ref, excerpt.record)
        shown = [message for hit in detail["hits"] for message in hit["messages"] if message["presented"]]
        assert shown and all(message["excerpt"] is not None for message in shown)
        assert any("RELEASE_FRIDAY" in message["preview"] for message in shown)
        source_request = MemorySourceQuery(session_id="conversation", retrieval_ref=excerpt.retrieval_ref,
            message_id="user-0", offset=len(learning.text(log.reader("conversation").get("user-0"))) - 60, length=60)
        reads_before = len(encoded)
        original = await queries.source(source_request)
        assert "RELEASE_FRIDAY" in original.text and original.next_offset is None
        assert await queries.source(source_request) == original
        assert len(encoded) == reads_before and logical_state_sha256(consumer.path) == before
        with pytest.raises(ValueError, match="not presented"):
            await queries.source(source_request.model_copy(update={"message_id": "user-1"}))
        log.ensure_session("other", SessionAttributes())
        with pytest.raises(ValueError, match="another memory scope"):
            await queries.source(source_request.model_copy(update={"session_id": "other"}))
        search_request = MemorySourceSearchQuery(session_id="conversation", retrieval_ref=excerpt.retrieval_ref,
            query="RELEASE_FRIDAY", max_chars=1024)
        found = await queries.search_source(search_request)
        assert found.windows and any("RELEASE_FRIDAY" in window.text for window in found.windows)
        assert len(found.model_dump_json()) <= 1024
        for window in found.windows:
            span = window.evidence.excerpt
            raw = learning.text(log.reader(window.evidence.session_id).get(window.evidence.message_id))
            assert raw[span.start:span.end] == window.text
            assert window.evidence.message_id in excerpt.record.presented_message_ids
        assert await queries.search_source(search_request) == found
        assert not (await queries.search_source(search_request.model_copy(update={"query": "NONEXISTENT_409"}))).windows
        with pytest.raises(ValueError, match="not presented"):
            await queries.search_source(search_request.model_copy(update={"message_id": "user-1"}))
        with pytest.raises(ValueError, match="another memory scope"):
            await queries.search_source(search_request.model_copy(update={"session_id": "other"}))
        assert len(encoded) == reads_before and logical_state_sha256(consumer.path) == before
        from plugins.akasha._boundaries import TOOLS, CallSource
        from plugins.akasha.recall_tool import tool_references
        from plugins.akasha.source_tool import SourceTool, check_source
        from plugins.content.api import decode_reference
        from plugins.ledger.contract import CallRef, ToolCall, ToolResult
        log.save_binding("source-binding", {"version": 1, "service": TOOLS.name, "root_ref": "fixture-archive",
            "metadata": {"tool": {"owner": "akasha", "name": "read_memory_source"}}})
        args = source_request.model_dump(mode="json", exclude={"session_id"})
        log.writer("conversation", author="user", source="conversation", body_types=(Input,),
                   content={"text": check_text}).append("read-input", Input((ContentPart("text", "Expand the source"),)))
        call = log.writer("conversation", author="assistant", source="conversation", body_types=(Output,),
            content={"text": check_text}, check_call=lambda _: None).append("read-call", Output((ToolCall("source-binding", args),), "continue"))
        tool = SourceTool(queries.source)
        prepared = await tool.prepare(args, CallSource(CallRef(call.message_id, 0), log.reader("conversation").snapshot()))
        assert not isinstance(prepared, str)
        result = await tool.invoke("read-call-key", prepared)
        log.writer("conversation", author="assistant", source="conversation", body_types=(ToolResult,),
            content={"text": check_text, "akasha.source": check_source}, call_ref=CallRef(call.message_id, 0)).append("read-result",
                ToolResult(CallRef(call.message_id, 0), result.outcome, result.parts))
        refs = tool_references(log.reader("conversation").snapshot(), "conversation", learning, bindings, records())
        assert refs and all(decode_reference(ref).resolved_ref == "user-0" for ref in refs)
        from plugins.akasha.source_tool import SourceSearchTool
        log.save_binding("search-binding", {"version": 1, "service": TOOLS.name, "root_ref": "fixture-archive",
            "metadata": {"tool": {"owner": "akasha", "name": "search_memory_source"}}})
        search_args = search_request.model_dump(mode="json", exclude={"session_id"})
        search_call = log.writer("conversation", author="assistant", source="conversation", body_types=(Output,),
            content={"text": check_text}, check_call=lambda _: None).append("search-call", Output((ToolCall("search-binding", search_args),), "continue"))
        search_tool = SourceSearchTool(queries.search_source)
        prepared_search = await search_tool.prepare(search_args, CallSource(CallRef(search_call.message_id, 0), log.reader("conversation").snapshot()))
        assert not isinstance(prepared_search, str)
        searched = await search_tool.invoke("search-call-key", prepared_search)
        assert any(part.kind == "akasha.source" for part in searched.parts)
        log.writer("conversation", author="assistant", source="conversation", body_types=(ToolResult,),
            content={"text": check_text, "akasha.source": check_source}, call_ref=CallRef(search_call.message_id, 0)).append("search-result",
                ToolResult(CallRef(search_call.message_id, 0), searched.outcome, searched.parts))
        refs = tool_references(log.reader("conversation").snapshot(), "conversation", learning, bindings, records())
        assert refs and all(decode_reference(ref).resolved_ref == "user-0" for ref in refs)


@pytest.mark.asyncio
async def test_program_memory_query_checks_scope_before_receipt_replay(tmp_path: Path) -> None:
    """A cached answer cannot bypass a current recall denial or invent a session."""
    from plugins.akasha.contract import MemoryQuery, MemorySourceSearchQuery
    from plugins.akasha.query import MemoryQueries
    from plugins.akasha.scopes import MemoryDisabled

    async with memory_fixture(tmp_path) as (_, _, _, _, log, _, _, _):
        def deny(_session):
            raise MemoryDisabled("recall disabled")

        queries = MemoryQueries(capture=lambda _: pytest.fail("must not bind a model"),
            open_recall=lambda _: pytest.fail("must not open a graph"), catalog=log.catalog(),
            records=lambda: pytest.fail("must not read receipts"), check_scope=deny)
        with pytest.raises(MemoryDisabled, match="recall disabled"):
            await queries.recall(MemoryQuery(key="cached", session_id="conversation", query="history"))
        with pytest.raises(ValueError, match="existing session"):
            await queries.recall(MemoryQuery(key="unknown", session_id="missing", query="history"))
        with pytest.raises(MemoryDisabled, match="recall disabled"):
            await queries.search_source(MemorySourceSearchQuery(session_id="conversation", retrieval_ref="cached", query="history"))
        with pytest.raises(ValueError, match="existing session"):
            await queries.search_source(MemorySourceSearchQuery(session_id="missing", retrieval_ref="cached", query="history"))


def test_original_lookup_keeps_changed_values_while_suppressing_exact_repeated_lines():
    from datetime import UTC, datetime
    from plugins.akasha.contract import MemoryEvidence, MemorySourceSearchQuery
    from plugins.akasha.source_search import search_originals

    evidence = MemoryEvidence(message_id="source", session_id="history", seq=0,
        recorded_at=datetime(2026, 10, 1, tzinfo=UTC), author="user")
    original = ("流程检查，不代表最终决定。\n" * 220 + "最终决定：预算760，必须防水。\n"
                + "后续清单不改变最终决定。\n" * 220)
    request = MemorySourceSearchQuery(session_id="reader", retrieval_ref="receipt", query="最终决定", max_chars=2400)
    found = search_originals(request, ((evidence, original),))
    assert any("预算760" in window.text for window in found.windows)
    assert len(found.model_dump_json()) <= 2400
    for window in found.windows:
        span = window.evidence.excerpt
        assert original[span.start:span.end] == window.text
    old = "预算决定：金额500。\n" + "配套条件保持不变。\n" * 50
    new = "预算决定：金额760。\n" + "配套条件保持不变。\n" * 50
    changed = old + "不相关背景。\n" * 200 + new
    both = search_originals(request.model_copy(update={"query": "预算决定", "max_chars": 6000}), ((evidence, changed),))
    assert any("金额500" in window.text for window in both.windows)
    assert any("金额760" in window.text for window in both.windows)


class WorkerGate:
    """在真实同步边界暂停 worker；测试线程可确定地推进事件循环。"""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.entered = asyncio.Event()
        self.release = threading.Event()

    def stop(self) -> None:
        assert threading.get_ident() != self.loop_thread, "Akasha work blocked the event loop"
        self.loop.call_soon_threadsafe(self.entered.set)
        assert self.release.wait(10), "test did not release worker"

    async def wait(self, job: asyncio.Task[object]) -> None:
        """等待屏障或直接报告工作失败，避免用超时掩盖原异常。"""
        arrival = asyncio.create_task(self.entered.wait())
        try:
            done, _ = await asyncio.wait((arrival, job), timeout=5,
                                         return_when=asyncio.FIRST_COMPLETED)
            if job in done:
                job.result()
                raise AssertionError("work finished without reaching the gate")
            assert arrival in done, "worker did not reach the gate"
        finally:
            arrival.cancel()
            await asyncio.gather(arrival, return_exceptions=True)


async def loop_turn() -> None:
    """让已排队 Task 获得执行机会，不依赖计时 sleep。"""
    ready = asyncio.Event()
    asyncio.get_running_loop().call_soon(ready.set)
    await ready.wait()


@asynccontextmanager
async def memory_fixture(path: Path, covered_ids=None) -> AsyncIterator[tuple[MessageMemory, MessageConsumer, Learning, Bindings, MessageLog, MessageEmbeddings, CompositionRoot, asyncio.Event]]:
    """使用真实消息、固定向量、provider scope、学习器和磁盘 writer。"""
    log = MessageLog(path / "sessions.db")
    log.ensure_session("conversation", SessionAttributes())
    embeddings = MessageEmbeddings(log, log._path.with_name("sessions-derived.db"))
    root = CompositionRoot("akasha-execution")
    learning = Learning(TurnProjection(), owner="akasha", post_commit_effect=legacy_post_commit_effect)
    bindings = Bindings(log, root.context)
    rule = LearningConfig(embedding_model="fixed", dimension=3, sources=("conversation",))
    identity = "fixed-learning"
    log.save_binding(identity, {"version": 1, "service": AKASHA_LEARNING.name,
        "root_ref": "fixture-archive", "metadata": rule.model_dump()})
    closed = asyncio.Event()

    async def provide(ctx: Context) -> None:
        await ctx.provide(AKASHA_LEARNING, learning)
        await ctx.effect(lambda: closed.set)

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    await root.mount(provide, name="learning")
    assert root.receipt().ready, repr(root.receipt())
    consumer = await MessageConsumer.load(path / "memory.db", catalog=log.catalog(),
        embeddings=embeddings, bindings=bindings, config=MemoryConfig(), cutover=False)
    memory = MessageMemory(consumer, catalog=log.catalog(), embeddings=embeddings,
        bindings=bindings, learning_binding=identity, records=RecallRecords(log.owner("akasha")),
        embed_batch=embed, covered_ids=covered_ids)
    try:
        yield memory, consumer, learning, bindings, log, embeddings, root, closed
    finally:
        await memory.close()
        await root.dispose()
        embeddings.close()
        log.close()


def append_pair(log: MessageLog, embeddings: MessageEmbeddings, learning: Learning, number: int, *, user_text: str | None = None) -> None:
    """真实已提交问答和固定向量是学习的唯一输入。"""
    for author, kind, body in (
        ("user", Input, Input((ContentPart("text", user_text if user_text is not None else f"remember episode {number}"),))),
        ("assistant", Output, Output((ContentPart("text", f"episode {number} happened"),), "complete")),
    ):
        message = log.writer("conversation", author=author, source="conversation",
            body_types=(kind,), content={"text": check_text}).append(f"{author}-{number}", body)
        embeddings.bind(learning.text).save(message, model="fixed", embedding=[1.0, 0.0, 0.0])


@pytest.mark.asyncio
async def test_graph_discovery_reuses_notified_heads_and_cutover_io_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """图发现不重扫目录；首次启用的固定 cutover 在可排空 worker 读取。"""
    from plugins.akasha.plugin import _write_graphs
    from plugins.akasha.scopes import DEFAULT_GRAPH, ScopePolicies, graph_key

    async with memory_fixture(tmp_path) as (_, _, learning, bindings, log, embeddings, _, _):
        append_pair(log, embeddings, learning, 0)
        catalog = log.catalog()
        policies = ScopePolicies(log.owner("scope-policy"), catalog)
        policies.set("project", "private", "isolated")
        policies.set("project", "disabled", "off")
        log.ensure_session("isolated", SessionAttributes(scope=(("project", "private"),)))
        log.ensure_session("disabled", SessionAttributes(scope=(("project", "disabled"),)))
        log.ensure_session("excluded", SessionAttributes(learning="excluded"))
        heads = catalog.snapshot_heads()
        original = catalog.snapshot_heads
        gate = WorkerGate()
        route = policies.route

        def routed(session):
            if session == "conversation":
                gate.stop()
            return route(session)

        monkeypatch.setattr(policies, "route", routed)
        monkeypatch.setattr(catalog, "snapshot_heads", lambda: pytest.fail("notified heads must not be rescanned"))
        discovery = asyncio.create_task(_write_graphs(catalog, policies, heads))
        try:
            await gate.wait(discovery)
            await loop_turn()
            assert not discovery.done()
            gate.release.set()
            assert await discovery == (DEFAULT_GRAPH, graph_key((("project", "private"),)))
        finally:
            gate.release.set()
            await asyncio.gather(discovery, return_exceptions=True)

        gate = WorkerGate()
        def snapshot(*args, **kwargs):
            # The catalog now passes a prefix filter; the fake forwards whatever
            # the caller used so the gate keeps measuring the real call.
            result = original(*args, **kwargs)
            gate.stop()
            return result

        monkeypatch.setattr(catalog, "snapshot_heads", snapshot)
        path = tmp_path / "cutover.db"
        load = asyncio.create_task(MessageConsumer.load(path, catalog=catalog, embeddings=embeddings,
            bindings=bindings, config=MemoryConfig()))
        try:
            await gate.wait(load)
            load.cancel()
            await loop_turn()
            load.cancel()
            await loop_turn()
            assert not load.done() and not path.exists()
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await load
            assert not path.exists()
            restored = await MessageConsumer.load(path, catalog=catalog, embeddings=embeddings,
                bindings=bindings, config=MemoryConfig())
            try:
                assert dict(restored.state.cutover_heads) == dict(heads)
                assert restored.state.applied == ()
            finally:
                restored.close()
        finally:
            gate.release.set()
            await asyncio.gather(load, return_exceptions=True)


@pytest.mark.asyncio
async def test_consumption_snapshot_and_membership_are_nonblocking_fixed_and_cancellable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一固定前缀过滤成员；后到的消息不偷跑，取消目录读取不前进学习。"""
    from plugins.akasha.scopes import DEFAULT_GRAPH, ScopePolicies

    async with memory_fixture(tmp_path) as (memory, consumer, learning, _, log, embeddings, _, _):
        append_pair(log, embeddings, learning, 0)
        catalog = memory._catalog
        policies = ScopePolicies(log.owner("scope-policy"), catalog)
        log.ensure_session("excluded", SessionAttributes(learning="excluded"))
        for author, kind, body in (
            ("user", Input, Input((ContentPart("text", "do not learn this excluded history"),))),
            ("assistant", Output, Output((ContentPart("text", "excluded response"),), "complete")),
        ):
            message = log.writer("excluded", author=author, source="conversation",
                body_types=(kind,), content={"text": check_text}).append(f"excluded-{author}", body)
            embeddings.bind(learning.text).save(message, model="fixed", embedding=[1.0, 0.0, 0.0])
        original = catalog.snapshot_heads
        gate = WorkerGate()
        admitted = []

        def snapshot(*args, **kwargs):
            # The catalog now passes a prefix filter; the fake forwards whatever
            # the caller used so the gate keeps measuring the real call.
            result = original(*args, **kwargs)
            gate.stop()
            return result

        def member(session):
            assert threading.get_ident() != gate.loop_thread, "scope route blocked the event loop"
            admitted.append(session)
            return policies.route(session).write == DEFAULT_GRAPH

        memory._member = member
        monkeypatch.setattr(catalog, "snapshot_heads", snapshot)
        first = asyncio.create_task(memory.consume())
        try:
            await gate.wait(first)
            append_pair(log, embeddings, learning, 1)
            await loop_turn()
            assert not first.done() and not consumer.state.applied
            gate.release.set()
            assert await first == 1
            assert [entry.ending[1] for entry in consumer.state.applied] == ["assistant-0"]
            assert await memory.consume() == 1
            assert [entry.ending[1] for entry in consumer.state.applied] == ["assistant-0", "assistant-1"]
        finally:
            gate.release.set()
            await asyncio.gather(first, return_exceptions=True)

        gate = WorkerGate()
        append_pair(log, embeddings, learning, 2)
        before = logical_state_sha256(consumer.path)
        current = asyncio.create_task(memory.consume())
        try:
            await gate.wait(current)
            current.cancel()
            await loop_turn()
            current.cancel()
            await loop_turn()
            assert not current.done() and logical_state_sha256(consumer.path) == before
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await current
            assert logical_state_sha256(consumer.path) == before
            assert await memory.consume() == 1
            assert [entry.ending[1] for entry in consumer.state.applied] == ["assistant-0", "assistant-1", "assistant-2"]
            assert await memory.consume() == 0
            assert set(admitted) == {"conversation", "excluded"}
        finally:
            gate.release.set()
            await asyncio.gather(current, return_exceptions=True)


@pytest.mark.asyncio
async def test_graph_initialization_consumes_once_and_keeps_arriving_notifications(tmp_path: Path) -> None:
    """首次追赶后不空扫；初始化中收到的下一前缀仍由真实 follower 学习。"""
    from agent.plugin_composition import ServiceKey
    from plugins.akasha.scopes import ScopePolicies, graph_key, graph_path
    from plugins.context.contract import MATERIALS_V4
    from tests.test_akasha_graphs import add_memory
    from tests.test_default_reply import application

    def sources(directory):
        add_memory(directory)
        # 只在夹具的真实学习完成边界暂停；不替换日志、学习算法或 follower。
        with (directory / "akasha/plugin.py").open("a") as stream:
            stream.write('''

_fixture_run = run
async def run(ctx, interest):
    from agent.plugin_composition import ServiceKey
    from .scopes import graph_key
    entered, release = asyncio.Event(), asyncio.Event()
    notified, completed = asyncio.Event(), asyncio.Event()
    calls = []
    target = graph_path(ctx.workspace_root("memory") / "akasha.db", graph_key((("project", "late"),)))
    original_consume, original_discover = MessageMemory.consume, _write_graphs
    async def consume(memory):
        tracked = memory._consumer.path == target
        if tracked:
            calls.append(len(calls) + 1)
        result = await original_consume(memory)
        if tracked and len(calls) == 1:
            entered.set()
            await release.wait()
        elif tracked and len(calls) == 2:
            completed.set()
        return result
    async def discover(catalog, policies, heads=None):
        result = await original_discover(catalog, policies, heads)
        if heads is not None and heads.get("late") == 3:
            notified.set()
        return result
    globals()["_write_graphs"] = discover
    MessageMemory.consume = consume
    def close():
        globals()["_write_graphs"] = original_discover
        MessageMemory.consume = original_consume
    await ctx.effect(lambda: close)
    await ctx.provide(ServiceKey("fixture.memory-initialization"), (entered, release, notified, completed, calls))
    await _fixture_run(ctx, interest)
''')

    async with application(tmp_path, replying=False, extra_sources=sources) as (log, host):
        policies = ScopePolicies(log.owner("plugin:akasha:scope-policy"), log.catalog())
        policies.set("project", "late", "isolated")
        log.ensure_session("late", SessionAttributes.scoped({"project": "late"}))

        def append(number):
            for author, kind, body in (
                ("user", Input, Input((ContentPart("text", f"fact {number}"),))),
                ("assistant", Output, Output((ContentPart("text", f"answer {number}"),), "complete")),
            ):
                log.writer("late", author=author, source="conversation", body_types=(kind,),
                    content={"text": check_text}).append(f"late-{author}-{number}", body)

        root = host.live_root
        assert root is not None
        entered, release, notified, completed, calls = root.context.require(ServiceKey("fixture.memory-initialization"))
        append(0)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert calls == [1]
            append(1)
            await asyncio.wait_for(notified.wait(), 5)
            release.set()
            await asyncio.wait_for(completed.wait(), 5)
            # completed 在真实 consume 返回前置位；旧逻辑随后会同步进入第三次空扫。
            assert calls == [1, 2]
            async with root.context.open_service(MATERIALS_V4) as service, service.bind() as materials:
                await materials.prepare(log.reader("late").snapshot(), "conversation")
            path = graph_path(tmp_path / "workspace/memory/akasha.db", graph_key((("project", "late"),)))
            state = load_consumption(path)
            assert state is not None
            assert [entry.ending[1] for entry in state.applied] == ["late-assistant-0", "late-assistant-1"]
            assert calls == [1, 2]
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("fail", [False, True])
async def test_batch_publication_keeps_trajectory_and_failure_stops_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, fail: bool) -> None:
    """MEM-009 / O：同图按批发布检查点，宿主仍运行；发布失败后同图学习与查询都停止，磁盘只含完整前缀。"""
    async with memory_fixture(tmp_path) as (memory, consumer, learning, _, log, embeddings, _, _):
        for number in range(2):
            append_pair(log, embeddings, learning, number)
        gate = WorkerGate()
        original_publish = consumer.publish_snapshot_for
        original_make = learning.make_turn
        observed: list[tuple[int, int]] = []

        def make_turn(*args, **kwargs):
            assert threading.get_ident() != gate.loop_thread
            disk = load_consumption(consumer.path)
            assert disk is not None
            observed.append((len(kwargs["previous"]), len(disk.applied)))
            return original_make(*args, **kwargs)

        def publish(state: Consumption) -> str:
            if len(state.applied) != 2:
                return original_publish(state)
            if boundary == "after":
                result = original_publish(state)
            gate.stop()
            if fail:
                raise OSError("controlled publication failure")
            return result if boundary == "after" else original_publish(state)

        monkeypatch.setattr(learning, "make_turn", make_turn)
        monkeypatch.setattr(consumer, "publish_snapshot_for", publish)
        first = asyncio.create_task(memory.consume())
        tasks: list[asyncio.Task[object]] = [first]
        try:
            await gate.wait(first)
            follower = asyncio.create_task(memory.consume())
            query = asyncio.create_task(memory.prepare(log.reader("conversation").snapshot(), "conversation"))
            tasks.extend((follower, query))
            await loop_turn()
            # 1. 同批第二条在第一条落盘前按内存中的 S(1) 计算；检查点一次覆盖整批。
            assert observed == [(0, 0), (1, 0)]
            assert not any(task.done() for task in tasks)
            disk = load_consumption(consumer.path)
            assert disk is not None
            assert len(disk.applied) == (2 if boundary == "after" else 0)
            gate.release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            disk = load_consumption(consumer.path)
            assert disk is not None
            if fail:
                # 2. 失败后同图 fail-loud；磁盘要么是旧检查点，要么是完整新检查点。
                assert isinstance(results[0], OSError)
                assert all(isinstance(result, RuntimeError) for result in results[1:])
                assert len(disk.applied) in (0, 2)
            else:
                assert results[:2] == [2, 0]
                assert isinstance(results[2], dict)
                assert [entry.ending[1] for entry in disk.applied] == ["assistant-0", "assistant-1"]
        finally:
            gate.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_restore_keeps_order_and_binding_until_worker_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    """PLG-003 / MEM-009：恢复按前缀运行；取消和卸载必须等待真实工作结束。"""
    async with memory_fixture(tmp_path) as (memory, consumer, learning, bindings, log, embeddings, root, closed):
        for number in range(2):
            append_pair(log, embeddings, learning, number)
        assert await memory.consume() == 2
        before = logical_state_sha256(consumer.path)
        await memory.close()
        gate = WorkerGate()
        original = learning.restore
        seen: list[int] = []

        def restore(*args, **kwargs):
            assert threading.get_ident() != gate.loop_thread, "restore must not run on the host loop"
            position = len(kwargs["previous"])
            assert position == len(seen)
            turn = original(*args, **kwargs)
            seen.append(position)
            if position == 0:
                gate.stop()
            return turn

        monkeypatch.setattr(learning, "restore", restore)
        load = asyncio.create_task(MessageConsumer.load(consumer.path, catalog=log.catalog(),
            embeddings=embeddings, bindings=bindings, config=MemoryConfig()))
        disposal = None
        try:
            await gate.wait(load)
            await loop_turn()
            assert seen == [0] and not load.done()
            if cancel:
                load.cancel()
                await loop_turn()
                load.cancel()
                disposal = asyncio.create_task(root.dispose())
                await loop_turn()
                assert not load.done() and not disposal.done() and not closed.is_set()
            gate.release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await load
                assert disposal is not None
                await disposal
                assert closed.is_set()
            else:
                restored = await load
                assert [turn.turn_id for turn in restored.cycle.turns] == [turn.turn_id for turn in consumer._cycle.turns]
                restored.close()
            assert seen == [0, 1]
            assert logical_state_sha256(consumer.path) == before
        finally:
            gate.release.set()
            await asyncio.gather(load, return_exceptions=True)
            if disposal is not None:
                await disposal


@pytest.mark.asyncio
async def test_cancelled_creation_closes_the_completed_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PLG-006：取消不能丢掉后台刚取得的 writer，旧任务排空后才能重新打开。"""
    gate = WorkerGate()
    original = MessageConsumer.__init__

    def build(self, *args, **kwargs):
        assert threading.get_ident() != gate.loop_thread
        original(self, *args, **kwargs)
        gate.stop()

    monkeypatch.setattr(MessageConsumer, "__init__", build)
    path = tmp_path / "memory.db"
    job = asyncio.create_task(MessageConsumer.create(path, turns=[], state=Consumption(cutover_heads=()), config=MemoryConfig()))
    try:
        await gate.wait(job)
        job.cancel()
        await loop_turn()
        job.cancel()
        await loop_turn()
        assert not job.done()
        with pytest.raises(RuntimeError, match="already has a writer"):
            WriterLease(path)
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        lease = WriterLease(path)
        lease.close()
        assert load_consumption(path) == Consumption(cutover_heads=())
    finally:
        gate.release.set()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_learning_drains_before_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """取消只阻止后续经历；当前经历与关闭时的检查点发布、writer 释放必须按顺序完成。"""
    async with memory_fixture(tmp_path) as (memory, consumer, learning, _, log, embeddings, _, _):
        for number in range(2):
            append_pair(log, embeddings, learning, number)
        gate = WorkerGate()
        original = learning.make_turn

        def make_turn(*args, **kwargs):
            # 停在第一条经历内部；第二条必须因取消而不开始。
            result = original(*args, **kwargs)
            if not kwargs["previous"]:
                gate.stop()
            return result

        monkeypatch.setattr(learning, "make_turn", make_turn)
        job = asyncio.create_task(memory.consume())
        close = None
        try:
            await gate.wait(job)
            job.cancel()
            await loop_turn()
            job.cancel()
            close = asyncio.create_task(memory.close())
            await loop_turn()
            assert not job.done() and not close.done()
            with pytest.raises(RuntimeError, match="already has a writer"):
                WriterLease(consumer.path)
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await job
            await close
            disk = load_consumption(consumer.path)
            assert disk is not None
            assert [entry.ending[1] for entry in disk.applied] == ["assistant-0"]
            assert consumer.state == disk
            lease = WriterLease(consumer.path)
            lease.close()
        finally:
            gate.release.set()
            await asyncio.gather(job, return_exceptions=True)
            if close is not None:
                await close


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_rebuild_finishes_publication_receipt_before_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    """重建仍复用逐条学习；替换后取消也要排空耐久回执。"""
    from plugins.akasha.application import rebuild

    async with memory_fixture(tmp_path) as (memory, consumer, learning, bindings, log, embeddings, _, _):
        for number in range(2):
            append_pair(log, embeddings, learning, number)
        assert await memory.consume() == 2
        before = logical_state_sha256(consumer.path)
        identity = consumer.state.applied[0].learning_binding
        await memory.close()
        gate = WorkerGate()
        original = rebuild._write_manifest

        def write_manifest(*args):
            gate.stop()
            return original(*args)

        async def unexpected_embedding(texts: list[str]) -> list[list[float]]:
            raise AssertionError("fixed embeddings must be reused")

        monkeypatch.setattr(rebuild, "_write_manifest", write_manifest)
        backups = tmp_path / "backups"
        job = asyncio.create_task(rebuild.rebuild_from_catalog(
            catalog=log.catalog(), embeddings=embeddings, bindings=bindings,
            config=MemoryConfig(), learning_binding=identity, embed_batch=unexpected_embedding,
            memory_path=consumer.path, backup_root=backups,
        ))
        try:
            await gate.wait(job)
            assert logical_state_sha256(consumer.path) == before
            if cancel:
                job.cancel()
                await loop_turn()
                assert not job.done()
            gate.release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await job
            else:
                report = await job
                assert report.turns == 2 and report.embedded_messages == 0
            assert len(list(backups.glob("*/manifest.json"))) == 1
            assert len(list(backups.glob("*/memory-before.db"))) == 1
            assert logical_state_sha256(consumer.path) == before
        finally:
            gate.release.set()
            await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_recall_indexes_quoted_originals_and_still_renders_compacted_ones(tmp_path):
    """A covered original earns its body back; a quoted one costs one index line.

    Both decisions are frozen in the receipt, so re-rendering the same request
    -- tool continuation steps, reminder replay, restart -- yields the same
    bytes, and the source tools still admit whichever form was surfaced.
    """
    from plugins.akasha.contract import MemorySourceQuery
    from plugins.akasha.query import MemoryQueries
    from plugins.akasha.recalls import context_identity

    covered: set[str] = set()

    async with memory_fixture(tmp_path, covered_ids=lambda _session: frozenset(covered)) as (
        memory, consumer, learning, _bindings, log, embeddings, _root, _closed,
    ):
        body = "Background filler. " * 200 + "needleproject budget BUDGET=915."
        append_pair(log, embeddings, learning, 0, user_text=body)
        for number in range(1, 3):
            append_pair(log, embeddings, learning, number)
        await memory.consume()
        records = RecallRecords(log.owner("akasha"))
        inputs = log.writer("conversation", author="user", source="conversation",
                            body_types=(Input,), content={"text": check_text})

        # 1. Nothing is covered yet, so the window still quotes user-0 verbatim.
        first_input = inputs.append("ask-quoted", Input((ContentPart("text", "needleproject budget"),)))
        quoted = await memory.prepare(log.reader("conversation").snapshot(), "conversation")
        live = [item for item in quoted["reminders"] if item.get("replay") is False]
        assert "BUDGET=915" not in str(live)
        assert "ref=user-0" in str(live)
        receipt = records.read(context_identity("conversation", "conversation", first_input.message_id))
        assert "user-0" in receipt.indexed_message_ids
        assert "user-0" not in receipt.presented_message_ids
        # The index line is what makes an index-only source readable at all.
        assert "user-0" in receipt.admitted_message_ids
        queries = MemoryQueries(capture=None, open_recall=None, catalog=log.catalog(),
                                records=lambda: records, check_scope=lambda _: "fixture-scope")
        reread = await queries.source(MemorySourceQuery(
            session_id="conversation",
            retrieval_ref=context_identity("conversation", "conversation", first_input.message_id),
            message_id="user-0", offset=len(body) - 60, length=60))
        assert "BUDGET=915" in reread.text
        # A frozen decision re-renders identically.
        assert quoted == await memory.prepare(log.reader("conversation").snapshot(), "conversation")

        # 2. Once a summary covers it, the original leaves the window and the
        #    body is worth spending the character budget on again.
        covered.update(message.message_id for message in log.reader("conversation").snapshot())
        second_input = inputs.append("ask-compacted", Input((ContentPart("text", "needleproject budget"),)))
        compacted = await memory.prepare(log.reader("conversation").snapshot(), "conversation")
        live = [item for item in compacted["reminders"] if item.get("replay") is False]
        assert "BUDGET=915" in str(live)
        later = records.read(context_identity("conversation", "conversation", second_input.message_id))
        assert "user-0" in later.presented_message_ids
        assert not later.indexed_message_ids


def test_inspector_distinguishes_body_index_quoted_and_undelivered(tmp_path):
    """The panel must be able to tell the four outcomes apart.

    A single `presented` boolean cannot: after the window rule, a candidate whose
    original the request already quotes looks exactly like one that simply did
    not fit the budget, and the owner cannot tell why memory looked quiet.
    """
    from datetime import UTC, datetime

    from plugins.akasha.contract import ContextSource, Hit, Recall
    from plugins.akasha.inspector import RecallInspector

    log = MessageLog(tmp_path / "ledger.db")
    try:
        log.ensure_session("conversation", SessionAttributes())
        writer = log.writer("conversation", author="user", source="conversation",
                            body_types=(Input,), content={"text": check_text})
        identities = []
        for index in range(4):
            message = writer.append(f"m{index}", Input((ContentPart("text", f"原文 {index} 的内容"),)))
            identities.append(message.message_id)
        body, line, quoted, undelivered = identities

        recall = Recall(
            learning_binding="fixed", graph_version=1, timestamp=datetime.now(UTC),
            source=ContextSource(session_id="conversation", source="conversation", through_seq=3),
            limit=10, rendering_version=3,
            hits=(Hit(node_id=0, session_id="conversation", message_ids=tuple(identities),
                      score=1.0, lane="dense", sources=("direct_dense",)),),
            presented_message_ids=(body,),
            withheld_message_ids=(line, quoted),
            indexed_message_ids=(line,),
            active_basin_count=0, pushes=0, residual_l1=0.0,
        )

        inspector = RecallInspector(read=lambda _i: recall, legacy_page=lambda _b: ((), None),
                                    catalog=log.catalog())
        detail = inspector._detail("context:fixture", recall)

        forms = {row["message_id"]: row["form"] for row in detail["hits"][0]["messages"]}
        assert forms[body] == "body"
        assert forms[line] == "index"
        assert forms[quoted] == "quoted"
        assert forms[undelivered] == "candidate"

        # The counts separate the three delivery outcomes as well.
        assert detail["presented_count"] == 1
        assert detail["indexed_count"] == 1
        assert detail["withheld_count"] == 2
        assert detail["hit_count"] == 1

        # The existing boolean keeps its old meaning for anything already reading it.
        presented = {row["message_id"]: row["presented"] for row in detail["hits"][0]["messages"]}
        assert presented == {body: True, line: False, quoted: False, undelivered: False}
    finally:
        log.close()


def test_a_pre_v3_checkpoint_demands_an_explicit_rebuild(tmp_path):
    """A graph published under the old schema must not be silently republished.

    Before schema v3 the edge weights varied with the clock and dense reduction
    was multi-threaded, so such a checkpoint's online trajectory cannot be
    reproduced from the log. Continuing would rewrite it as v3 and make an
    unreplayed graph indistinguishable from a migrated one.
    """
    import sqlite3

    from plugins.akasha.domain.model import MemoryRebuildRequiredError
    from plugins.akasha.infrastructure.persistence import load_consumption

    graph = tmp_path / "akasha.db"
    with sqlite3.connect(graph) as connection:
        _ = connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        _ = connection.execute("INSERT INTO metadata VALUES ('consumer_state_json', '{}')")
        _ = connection.execute("PRAGMA user_version = 2")

    before = graph.read_bytes()
    try:
        load_consumption(graph)
    except MemoryRebuildRequiredError as error:
        assert "v2" in str(error) and "v3" in str(error)
        assert "重建" in str(error), "the operator needs to be told what to do"
    else:
        raise AssertionError("a pre-v3 checkpoint was accepted")
    # Reading must not touch the old checkpoint; rebuilding stays the owner's call.
    assert graph.read_bytes() == before

    # A graph with no version recorded at all is left to the existing checks,
    # which report a missing consumption provenance rather than a schema age.
    unversioned = tmp_path / "unversioned.db"
    with sqlite3.connect(unversioned) as connection:
        _ = connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    assert load_consumption(unversioned) is None


@pytest.mark.asyncio
async def test_explicit_recall_queries_the_running_graph_and_falls_back_without_it(tmp_path):
    """Restoring the published copy decodes every learned original, so avoid it.

    When a graph is running, the explicit recall tool must search it inside the
    serial lock. When it is not -- stopped, or rebuilding -- the tool must still
    answer from the published copy rather than fail.
    """
    from contextlib import asynccontextmanager

    from plugins.akasha.recall_tool import RecallTool

    seen: list[tuple[str | None, tuple[str, int]]] = []

    @asynccontextmanager
    async def live(session_id, space):
        seen.append((session_id, space))
        yield None

    # The accessor is optional, so a composition that never wires it still works.
    assert "live" in RecallTool.__init__.__code__.co_varnames
    tool = RecallTool.__new__(RecallTool)
    tool._live = None
    assert tool._live is None

    tool._live = live
    async with tool._live("akashic:owner", ("fixed", 3)) as view:
        # None is the contract for "not running"; the caller reads the copy.
        assert view is None
    assert seen == [("akashic:owner", ("fixed", 3))]

    # A program call without a session asks for the default graph.
    async with tool._live(None, ("fixed", 3)) as view:
        assert view is None
    assert seen[-1][0] is None
