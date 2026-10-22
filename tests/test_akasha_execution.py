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
async def memory_fixture(path: Path) -> AsyncIterator[tuple[MessageMemory, MessageConsumer, Learning, Bindings, MessageLog, MessageEmbeddings, CompositionRoot, asyncio.Event]]:
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
        embed_batch=embed)
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
