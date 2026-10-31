from plugins.ledger.contract import ContentReferences
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from plugins.ledger.log import MessageConflict, MessageLog, SessionAttributes
from plugins.ledger.contract import CallRef, ContentPart, Input, Output, ToolCall, ToolResult

def text_schema(part):
    if not isinstance(part.value, str):
        raise ValueError("text 必须是字符串")
    return ContentReferences()

def writer(
    log,
    *,
    source="conversation",
    author="user",
    bodies=(Input,),
    call_ref=None,
    check_call=None,
):
    return log.writer(
        "s",
        author=author,
        source=source,
        body_types=bodies,
        content={"text": text_schema},
        call_ref=call_ref,
        check_call=check_call,
    )

@pytest.fixture
def log(tmp_path):
    result = MessageLog(tmp_path / "sessions.db")
    try:
        yield result
    finally:
        result.close()

def test_concurrent_writers_allocate_one_sequence_per_fact(log):
    barrier = Barrier(2)

    def append(source):
        bound = writer(log, source=source)
        barrier.wait()
        return bound.append(source, Input(())).seq

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(append, ("one", "two"))) == [0, 1]
    assert len(log.reader("s").read()) == 2

def test_missing_resource_rolls_back_message_and_sequence(log):
    outputs = writer(
        log, author="agent", bodies=(Output,), check_call=lambda call: None
    )
    body = Output((ToolCall("missing", {}),), "continue")
    with pytest.raises(sqlite3.IntegrityError):
        outputs.append("call", body)
    assert log.reader("s").read() == ()
    log.save_binding("missing", {"artifact": "immutable-revision"})
    assert outputs.append("call", body).seq == 0
    with pytest.raises(MessageConflict):
        log.save_binding("missing", {"artifact": "new-revision"})


def test_committed_binding_reuse_avoids_sql_and_rejects_descriptor_drift(log, monkeypatch):
    descriptor = {"nested": {"revision": [1, 2]}}
    log.save_binding("immutable", descriptor)
    statements = []
    log._writer_connection.set_trace_callback(statements.append)
    try:
        for _ in range(20):
            log.save_binding("immutable", {"nested": {"revision": [1, 2]}})
            assert log.read_binding("immutable")["nested"]["revision"] == (1, 2)
        with pytest.raises(MessageConflict):
            log.save_binding("immutable", {"nested": {"revision": [3]}})
        assert statements == []
    finally:
        log._writer_connection.set_trace_callback(None)
    monkeypatch.setattr("plugins.ledger.log._BINDING_MEMO_SIZE", 2)
    log.save_binding("second", {"revision": 2})
    log.save_binding("third", {"revision": 3})
    assert len(log._binding_payloads) == len(log._binding_cache) == 2
    assert "immutable" not in log._binding_cache
    assert log.read_binding("immutable")["nested"]["revision"] == (1, 2)
    with pytest.raises(MessageConflict):
        log.save_binding("immutable", {"revision": "drift after eviction"})


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["gateway", "ui-follow", "cold-catalog"])
async def test_public_message_reads_keep_loop_live_and_drain_cancellation(log, monkeypatch, tmp_path, boundary):
    import asyncio
    from plugins.gateway.service import ControlService
    from plugins.ledger.log import MessageCatalog, MessageReader
    from plugins.ui.contract import follow_messages
    from tests.test_akasha_execution import WorkerGate, loop_turn

    writer(log).append("first", Input(()))
    catalog, reader = log.catalog(), log.reader("s")
    gate = WorkerGate()
    target = MessageCatalog if boundary == "cold-catalog" else MessageReader
    method = "snapshot_heads" if boundary == "cold-catalog" else "read_page"
    original = getattr(target, method)

    def slow_read(current, *args, **kwargs):
        with log._view_lock:
            gate.stop()
        return original(current, *args, **kwargs)

    monkeypatch.setattr(target, method, slow_read)
    service = ControlService(catalog, tmp_path, accept=None, reply_status=None,
                             attachments=None, control_frames=None)

    async def read():
        if boundary == "gateway":
            return await service.read_messages("s", -1, None, 50)
        feed = catalog.follow() if boundary == "cold-catalog" else follow_messages(reader, after_seq=-1)
        try:
            return await anext(feed)
        finally:
            await feed.aclose()

    def begin():
        if boundary == "cold-catalog":
            # A restarted log has no in-process committed-head cache yet.
            with log._listener_lock:
                log._heads = None
        return asyncio.create_task(read())

    job = begin()
    try:
        await gate.wait(job)
        await loop_turn()
        job.cancel()
        await loop_turn()
        job.cancel()
        await loop_turn()
        assert not job.done()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
    finally:
        gate.release.set()
        await asyncio.gather(job, return_exceptions=True)

    gate = WorkerGate()
    job = begin()
    try:
        await gate.wait(job)
        await loop_turn()
        assert not job.done()
        gate.release.set()
        page = await job
        if boundary == "cold-catalog":
            assert page == {"s": 0}
        else:
            assert page["after_seq"] == -1 and page["through_seq"] == 0
            assert [row["id"] for row in page["items"]] == ["first"]
    finally:
        gate.release.set()
        await asyncio.gather(job, return_exceptions=True)
    if boundary == "cold-catalog":
        gate = WorkerGate()
        feed = catalog.follow()
        cached = asyncio.create_task(anext(feed))
        try:
            await loop_turn()
            assert cached.done() and not gate.entered.is_set()
            assert await cached == {"s": 0}
        finally:
            gate.release.set()
            await asyncio.gather(cached, return_exceptions=True)
            await feed.aclose()
    if boundary == "ui-follow":
        inputs = writer(log)
        for number in range(1, 55):
            inputs.append(f"page-{number}", Input(()))
        feed = follow_messages(reader, after_seq=-1)
        try:
            first = await anext(feed)
            assert len(first["items"]) == 50 and first["through_seq"] == 54
            inputs.append("arrived-during-page", Input(()))
            second = await anext(feed)
            assert second["through_seq"] == 54 and [row["seq"] for row in second["items"]] == list(range(50, 55))
            third = await anext(feed)
            assert third["after_seq"] == 54 and [row["id"] for row in third["items"]] == ["arrived-during-page"]
        finally:
            await feed.aclose()
    assert not log._listeners


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dispatch", "admission"])
async def test_reply_follower_reads_drain_off_loop_and_recheck_source_registration(log, monkeypatch, boundary):
    """物理读取持锁时宿主仍可运行；取消先排空，旧来源不能越过等待边界。"""
    import asyncio
    import threading
    from types import SimpleNamespace
    from plugins.reply.follow import _Follower, _Wake
    from tests.test_akasha_execution import WorkerGate, loop_turn

    writer(log).append("first", Input(()))
    old_source = SimpleNamespace(name="conversation")
    new_source = SimpleNamespace(name="conversation")
    registrations = [old_source]
    follower = _Follower(None, log.catalog(), SimpleNamespace(entries=lambda: tuple(registrations)), None, None)
    scheduled, started = [], []
    loop_thread = threading.get_ident()

    def schedule(session, source):
        assert threading.get_ident() == loop_thread, "only the loop may schedule a source"
        scheduled.append((session, source))

    async def start(state, session, source):
        started.append((session, source))
        state.stop_drive = True

    monkeypatch.setattr(follower, "_schedule", schedule)
    monkeypatch.setattr(follower, "_start", start)
    reader_type = type(log.reader("s"))
    original_head, original_names = reader_type.head, reader_type.source_names
    gate = WorkerGate()

    def head(reader, **options):
        assert threading.get_ident() != loop_thread, "source head blocked the loop"
        # 模拟一次确实持有共享存储准入锁的慢 I/O，不替换读取结果。
        with log._read_admission:
            gate.stop()
        return original_head(reader, **options)

    def names(reader):
        assert threading.get_ident() != loop_thread, "source discovery blocked the loop"
        return original_names(reader)

    monkeypatch.setattr(reader_type, "head", head)
    monkeypatch.setattr(reader_type, "source_names", names)
    wake = _Wake(source=old_source)

    def operation(source=old_source, sequence=0):
        return asyncio.create_task(follower._dispatch_changes({"s": sequence}) if boundary == "dispatch"
                                   else follower._drive_source("s", source, wake))

    job = operation()
    try:
        await gate.wait(job)
        await loop_turn()  # 与持锁 worker 同时推进的真实事件循环心跳。
        job.cancel()
        await loop_turn()
        job.cancel()
        await loop_turn()
        assert not job.done() and not scheduled and not started
        assert not follower._previous_heads and not follower._observed_source_heads
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
    finally:
        gate.release.set()
        await asyncio.gather(job, return_exceptions=True)

    gate = WorkerGate()
    job = operation()
    try:
        await gate.wait(job)
        registrations[:] = [new_source]
        gate.release.set()
        result = await job
        assert not scheduled and not started
        if boundary == "dispatch":
            assert not follower._previous_heads and not follower._observed_source_heads
            assert follower._wake_event.is_set()
        else:
            assert result is True and wake.source is new_source and wake.changed
        # 来源替换与新输入都未被旧读取消耗；下一次仍读取真实最新事实。
        await writer(log).append_async("second", Input(()))
        await operation(new_source, 1)
        if boundary == "dispatch":
            assert scheduled == [("s", new_source)]
            assert follower._observed_source_heads == {("s", "conversation"): 1}
        else:
            assert started == [("s", new_source)]
    finally:
        gate.release.set()
        await asyncio.gather(job, return_exceptions=True)

    if boundary == "dispatch":
        await writer(log).append_async("growth-third", Input(()))
        gate = WorkerGate()
        job = operation(new_source, 2)
        following = None
        try:
            await gate.wait(job)
            # 模拟 watcher 在旧目录读取期间收到下一 head：它只置 dirty，不能
            # 被这次读取完成后的旧快照覆盖；真实新输入也并发提交。
            follower._latest_heads = {"s": 3}
            follower._wake_event.set()
            following = asyncio.create_task(writer(log).append_async("growth-fourth", Input(())))
            gate.release.set()
            await job
            assert (await following).seq == 3
            assert follower._previous_heads == {"s": 2}
            assert follower._observed_source_heads == {("s", "conversation"): 2}
            assert follower._latest_heads == {"s": 3} and follower._wake_event.is_set()
            await follower._dispatch_changes(follower._latest_heads)
            assert follower._observed_source_heads == {("s", "conversation"): 3}
        finally:
            gate.release.set()
            await asyncio.gather(*(item for item in (job, following) if item is not None), return_exceptions=True)

    previous_heads = dict(follower._previous_heads)
    previous_observed = dict(follower._observed_source_heads)
    third = await writer(log).append_async("third", Input(()))
    gate = WorkerGate()
    job = operation(new_source, third.seq)
    try:
        await gate.wait(job)
        registrations.clear()
        gate.release.set()
        result = await job
        if boundary == "dispatch":
            assert follower._previous_heads == previous_heads
            assert follower._observed_source_heads == previous_observed
            follower._active["s", "conversation"] = wake
            wake.source = new_source
            await operation(new_source, third.seq)
            assert wake.source is None and wake.changed
        else:
            assert result is False and wake.source is None
        assert scheduled == ([("s", new_source)] * 3 if boundary == "dispatch" else [])
        assert started == ([("s", new_source)] if boundary == "admission" else [])
    finally:
        gate.release.set()
        await asyncio.gather(job, return_exceptions=True)


def test_failed_binding_write_is_not_cached_and_reopened_data_remains_authoritative(tmp_path):
    path = tmp_path / "bindings.db"
    log = MessageLog(path)
    try:
        def reject_insert(action, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK
        log._writer_connection.set_authorizer(reject_insert)
        with pytest.raises(sqlite3.DatabaseError):
            log.save_binding("retried", {"revision": 1})
        log._writer_connection.set_authorizer(None)
        with pytest.raises(KeyError):
            log.read_binding("retried")
        log.save_binding("retried", {"revision": 2})
    finally:
        log.close()
    reopened = MessageLog(path)
    try:
        assert reopened.read_binding("retried")["revision"] == 2
        with pytest.raises(MessageConflict):
            reopened.save_binding("retried", {"revision": 1})
        reopened.save_binding("retried", {"revision": 2})
    finally:
        reopened.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        reopened.save_binding("retried", {"revision": 2})

@pytest.mark.asyncio
async def test_session_scope_is_fixed_at_admission_and_absent_for_old_sessions(log, tmp_path, monkeypatch):
    import asyncio
    _ = writer(log).append("m1", Input(()))
    scoped = SessionAttributes.scoped({"project": "p_1"})
    initialized = []
    initializers = ((log.owner("fixture"), lambda sid, attrs, tx: initialized.append((sid, attrs))),)
    admitted = await asyncio.gather(*(log.ensure_session_async("web:a", scoped, initializers=initializers)
                                     for _ in range(2)))
    assert admitted == [scoped, scoped] and initialized == [("web:a", scoped)]

    def reject_write(_):
        raise AssertionError("existing immutable attributes must not open a writer transaction")

    with monkeypatch.context() as patched:
        patched.setattr(log, "_write", reject_write)
        assert log.ensure_session("web:a", scoped) == scoped
        assert await log.ensure_session_async("web:a", scoped, initializers=initializers) == scoped
        with pytest.raises(MessageConflict):
            _ = log.ensure_session("web:a", SessionAttributes.scoped({"project": "p_2"}))
        with pytest.raises(MessageConflict):
            _ = log.ensure_session("s", scoped)
        with log.reader("web:a").read_snapshot(), pytest.raises(RuntimeError, match="active storage transaction"):
            _ = log.ensure_session("web:a", scoped)
    with closing(sqlite3.connect(tmp_path / "sessions.db")) as raw:
        rows = dict(raw.execute("SELECT key, attributes FROM sessions").fetchall())
    assert rows["s"] == '{"learning": "eligible", "visibility": "listed"}'
    assert SessionAttributes().dimension("project") == "default"


def _call_message(log, message_id="call", source="conversation", parts=None):
    log.save_binding("tool", {"artifact": "immutable-revision"})
    outputs = writer(
        log, source=source, author="agent", bodies=(Output,),
        check_call=lambda call: None,
    )
    return outputs.append(
        message_id, Output(parts or (ToolCall("tool", {}),), "continue")
    )


def _result_writer(log, ref, source="conversation"):
    return writer(
        log, source=source, author="tool", bodies=(ToolResult,), call_ref=ref,
    )


def test_tool_result_commits_against_real_tool_call(log):
    _ = _call_message(log)
    ref = CallRef("call", 0)
    result = _result_writer(log, ref).append("result", ToolResult(ref, "success", ()))
    assert result.seq == 1
    with pytest.raises(MessageConflict, match="已经有结果消息"):
        _result_writer(log, ref).append("result2", ToolResult(ref, "success", ()))


def test_tool_result_rejects_unknown_or_foreign_call(log):
    _ = _call_message(log)
    missing = CallRef("missing", 0)
    with pytest.raises(ValueError, match="调用不在 writer 获授的 Session/source 内"):
        _result_writer(log, missing).append("r1", ToolResult(missing, "success", ()))
    foreign = CallRef("foreign-call", 0)
    with pytest.raises(ValueError, match="调用不在 writer 获授的 Session/source 内"):
        _result_writer(log, foreign, source="tools").append(
            "r2", ToolResult(foreign, "success", ())
        )
    # 调用真实存在但属于另一来源：结果 writer 不得跨来源写入。
    _ = _call_message(log, "other-call", source="other")
    other = CallRef("other-call", 0)
    with pytest.raises(ValueError, match="调用不在 writer 获授的 Session/source 内"):
        _result_writer(log, other).append("r3", ToolResult(other, "success", ()))


def test_tool_result_rejects_non_call_target(log):
    _ = writer(log).append("input", Input(()))
    _ = _call_message(
        log, "mixed",
        parts=(ToolCall("tool", {}), ContentPart("text", "说明")),
    )
    input_ref = CallRef("input", 0)
    with pytest.raises(ValueError, match="call_ref 未指向真实工具调用"):
        _result_writer(log, input_ref).append("r1", ToolResult(input_ref, "success", ()))
    text_ref = CallRef("mixed", 1)
    with pytest.raises(ValueError, match="call_ref 未指向真实工具调用"):
        _result_writer(log, text_ref).append("r2", ToolResult(text_ref, "success", ()))
    out_of_range = CallRef("mixed", 2)
    with pytest.raises(ValueError, match="call_ref 未指向真实工具调用"):
        _result_writer(log, out_of_range).append(
            "r3", ToolResult(out_of_range, "success", ())
        )
