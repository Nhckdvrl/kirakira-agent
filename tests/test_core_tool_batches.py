"""Main-chain concurrency, ownership and recovery contracts, with real SQLite."""

import asyncio
import threading

import pytest

from agent.core.admission import TurnAdmission
from agent.core.contracts import RuntimeConfig
from agent.core.execution import ExecutionStore
from agent.core.model_io import ModelInvoker
from agent.plugins.snapshot import (
    RuntimeSnapshotStore,
    bind_runtime_snapshot,
    compile_snapshot,
    get_current_runtime_snapshot,
    reset_runtime_snapshot,
)
from agent.tool_hooks import HookOutcome
from agent.tools.registry import ToolRegistry
from agent.turns.policy import ReasoningPolicy
from bus.event_bus import EventBus
from bus.events import InboundMessage
from bus.events_lifecycle import StreamDeltaReady
from core.schema import ModelResponse, ToolCall, ToolSpec
from session.manager import SessionManager
from tests.test_core_execution import Model, reasoner, run
from tests.test_runtime import build_test_runtime


def register(tools, name, handler, *, parallel=False, risk="read-only"):
    tools.register(
        ToolSpec(name, name, {"type": "object"}),
        handler,
        parallel_safe=parallel,
        risk=risk,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_reads_overlap_with_ordered_results_and_write_barriers(tmp_path, limit):
    tools, trace = ToolRegistry(), []
    active, peak = 0, 0

    async def read(index):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        trace.append(("start", index))
        try:
            # Later calls finish sooner; model results must retain call order.
            await asyncio.sleep((8 - index) * 0.003)
            return str(index)
        finally:
            trace.append(("end", index))
            active -= 1

    def write():
        assert active == 0
        trace.append(("write", None))
        return "written"

    register(tools, "read", read, parallel=True)
    register(tools, "write", write, risk="write")
    calls = [ToolCall(str(i), "read", {"index": i}) for i in range(4)]
    calls += [ToolCall("w", "write", {})]
    calls += [ToolCall(str(i), "read", {"index": i}) for i in range(4, 8)]
    engine = reasoner(
        tmp_path,
        Model(ModelResponse(tool_calls=calls), ModelResponse(text="done")),
        tools,
    )
    engine.config.max_parallel_tool_calls = limit
    try:
        result = await run(engine)
        assert peak == limit
        assert [c["call_id"] for c in result.tool_chain[0]["calls"]] == [
            c.id for c in calls
        ]
        assert all(c["status"] == "success" for c in result.tool_chain[0]["calls"])
        barrier = trace.index(("write", None))
        assert all(trace.index(("end", i)) < barrier for i in range(4))
        assert all(trace.index(("start", i)) > barrier for i in range(4, 8))
        assert engine.execution_store.unresolved(result.execution_id) == []
        facts = engine.execution_store.events(result.execution_id)
        assert sum(f["kind"] == "tool_intent" for f in facts) == 9
        assert sum(f["kind"] == "tool_result" for f in facts) == 9
    finally:
        engine.execution_store.close()
        await tools.shutdown()


@pytest.mark.asyncio
async def test_parallel_pool_reuses_a_free_slot_before_slow_predecessor_finishes(
    tmp_path,
):
    tools = ToolRegistry()
    next_started, release = asyncio.Event(), asyncio.Event()

    async def read(index):
        if index == 0:
            await release.wait()
        elif index == 2:
            next_started.set()
        return str(index)

    register(tools, "read", read, parallel=True)
    engine = reasoner(
        tmp_path,
        Model(
            ModelResponse(
                tool_calls=[ToolCall(str(i), "read", {"index": i}) for i in range(3)]
            ),
            ModelResponse(text="done"),
        ),
        tools,
    )
    engine.config.max_parallel_tool_calls = 2
    task = asyncio.create_task(run(engine))
    try:
        await asyncio.wait_for(next_started.wait(), 3)
        assert not task.done()
        release.set()
        result = await task
        assert [c["call_id"] for c in result.tool_chain[0]["calls"]] == ["0", "1", "2"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        engine.execution_store.close()
        await tools.shutdown()


def test_writes_cannot_opt_into_parallel_reads():
    for risk in ("write", "external-side-effect"):
        with pytest.raises(ValueError, match="read-only"):
            register(ToolRegistry(), "write", lambda: None, parallel=True, risk=risk)


@pytest.mark.asyncio
async def test_parallel_hook_grants_loop_guard_and_terminal_exclusivity(tmp_path):
    tools, effects = ToolRegistry(), []
    register(tools, "read", lambda **kw: effects.append(kw) or "read", parallel=True)
    register(tools, "finish", lambda: effects.append("finish") or "finished")
    calls = [ToolCall(str(i), "read", {"index": i}) for i in range(2)]
    calls += [ToolCall(str(i + 2), "read", {"index": 2}) for i in range(4)]
    engine = reasoner(
        tmp_path,
        Model(ModelResponse(tool_calls=calls), ModelResponse(text="done")),
        tools,
    )

    class Deny:
        name, event = "deny-zero", "pre_tool_use"

        def matches(self, ctx):
            return ctx.current_arguments["index"] == 0

        async def run(self, ctx):
            return HookOutcome(decision="deny", reason="test denied")

    engine.add_tool_hooks([Deny()])
    try:
        result = await run(engine)
        rows = result.tool_chain[0]["calls"]
        assert [c["status"] for c in rows] == [
            "denied",
            "success",
            "success",
            "success",
            "success",
            "denied",
        ]
        assert len(effects) == 4
        assert (
            len(
                [
                    e
                    for e in engine.execution_store.events(result.execution_id)
                    if e["kind"] == "tool_intent"
                ]
            )
            == 4
        )
        engine._model_io.model_client = Model(
            ModelResponse(tool_calls=[*calls[:2], ToolCall("t", "finish", {})]),
            ModelResponse(text="denied"),
        )
        result = await run(
            engine,
            policy=ReasoningPolicy(
                terminal_tools=frozenset({"finish"}), exclusive_terminal=True
            ),
        )
        assert all(c["status"] == "denied" for c in result.tool_chain[0]["calls"])
        assert len(effects) == 4
    finally:
        engine.execution_store.close()
        await tools.shutdown()


@pytest.mark.asyncio
async def test_parallel_children_keep_snapshot_and_retired_generation_until_drain(
    tmp_path,
):
    entered, release = asyncio.Event(), asyncio.Event()
    seen, drained = [], []
    tools = ToolRegistry()
    old = compile_snapshot(revision="old")
    store = RuntimeSnapshotStore(on_drain=lambda snapshot: record_drain(snapshot))

    async def record_drain(snapshot):
        drained.append(snapshot.snapshot_id)

    await store.commit(store.publish(old))

    async def read(index):
        seen.append(get_current_runtime_snapshot().snapshot_id)
        if len(seen) == 2:
            entered.set()
        await release.wait()
        assert get_current_runtime_snapshot() is old
        return str(index)

    register(tools, "read", read, parallel=True)
    engine = reasoner(
        tmp_path,
        Model(
            ModelResponse(
                tool_calls=[ToolCall(str(i), "read", {"index": i}) for i in range(2)]
            ),
            ModelResponse(text="done"),
        ),
        tools,
    )

    async def turn():
        lease = store.lease()
        token = bind_runtime_snapshot(lease)
        try:
            return await run(engine)
        finally:
            reset_runtime_snapshot(token)
            await lease.release()

    task = asyncio.create_task(turn())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        new = compile_snapshot(revision="new")
        await store.commit(store.publish(new))
        assert drained == [] and old.lease_count == 3
        release.set()
        result = await task
        assert seen == [old.snapshot_id, old.snapshot_id]
        assert drained == [old.snapshot_id]
        assert engine.execution_store.unresolved(result.execution_id) == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        engine.execution_store.close()
        await tools.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_repeated_cancel_keeps_sync_tool_session_owner_until_thread_stops(
    tmp_path,
):
    entered, release, physically_stopped = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    model = Model(
        ModelResponse(tool_calls=[ToolCall("s", "slow", {})]),
        ModelResponse(text="next"),
    )
    _, loop, sessions, _ = build_test_runtime(tmp_path, model)
    engine = loop.pipeline.reasoner
    engine.execution_store = ExecutionStore(tmp_path / "execution.db")

    def slow():
        entered.set()
        try:
            assert release.wait(5)
            (tmp_path / "effect").write_text("once")
        finally:
            physically_stopped.set()

    register(engine.tools, "slow", slow, risk="write")

    async def turn(text):
        return await loop.pipeline.run(
            InboundMessage("cli", "u", "1", text), "cli:1", dispatch_outbound=False
        )

    first = asyncio.create_task(turn("first"))
    second = None
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        first.cancel()
        await asyncio.sleep(0.01)
        first.cancel()
        second = asyncio.create_task(turn("second"))
        await asyncio.sleep(0.02)
        assert loop.is_busy("cli:1") and not first.done() and model.calls == 1
        assert not physically_stopped.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert physically_stopped.is_set()
        await second
        assert (tmp_path / "effect").read_text() == "once"
        statuses = {r["status"] for r in engine.execution_store.recent()}
        assert statuses == {"needs_reconciliation", "completed"}
    finally:
        release.set()
        await asyncio.gather(
            *(t for t in (first, second) if t is not None), return_exceptions=True
        )
        engine.execution_store.close()
        sessions.close()
        await engine.tools.shutdown()


@pytest.mark.asyncio
async def test_parallel_intent_failure_cancels_siblings_and_does_not_cross_write_barrier(
    tmp_path,
):
    tools, effects, stopped = ToolRegistry(), [], asyncio.Event()

    async def read(index):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    register(tools, "read", read, parallel=True)
    register(tools, "write", lambda: effects.append("wrong"), risk="write")
    engine = reasoner(
        tmp_path,
        Model(
            ModelResponse(
                tool_calls=[
                    ToolCall("0", "read", {"index": 0}),
                    ToolCall("1", "read", {"index": 1}),
                    ToolCall("w", "write", {}),
                ]
            )
        ),
        tools,
    )
    append = engine.execution_store.append

    def failing(run_id, kind, payload):
        if kind == "tool_intent" and payload["call_id"] == "1":
            raise OSError("journal unavailable")
        append(run_id, kind, payload)

    engine.execution_store.append = failing
    try:
        with pytest.raises(ExceptionGroup) as error:
            await run(engine)
        assert any(isinstance(exc, OSError) for exc in error.value.exceptions)
        assert stopped.is_set() and effects == []
        row = engine.execution_store.recent()[0]
        assert row["status"] == "needs_reconciliation"
        assert [
            p["call_id"] for p in engine.execution_store.unresolved(row["run_id"])
        ] == ["0"]
    finally:
        engine.execution_store.close()
        await tools.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [0, 3, 2000])
async def test_stream_completion_delivers_all_queued_callbacks_in_order(chunks):
    deltas = []

    class Stream:
        def complete_stream(self, *args, **kwargs):
            raise AssertionError("async stream required")

        async def acomplete_stream(self, *args, **kwargs):
            for index in range(chunks):
                args[5](str(index), "")
            return ModelResponse(text="done")

    bus = EventBus()
    bus.on(StreamDeltaReady, lambda event: deltas.append(event.content_delta))
    result = await ModelInvoker(Stream(), RuntimeConfig(model="fake"), bus).complete(
        [], [], session_key="s", channel="c", chat_id="1", iteration=0
    )
    assert result.text == "done" and deltas == [str(i) for i in range(chunks)]


def test_filtered_journal_preserves_old_database_and_recovery(tmp_path):
    path = tmp_path / "execution.db"
    store = ExecutionStore(path)
    run_id = store.start(session_key="s", scope="test", parent_id=None, inputs={})
    store.append(run_id, "model_request", {"messages": "x" * 100_000})
    store.append(run_id, "tool_intent", {"operation_id": "a", "call_id": "a"})
    store.append(run_id, "tool_intent", {"operation_id": "b", "call_id": "b"})
    store.append(run_id, "tool_result", {"operation_id": "b", "content": "y" * 100_000})
    store.db.execute("DROP INDEX execution_events_kind")
    store.db.commit()
    original = store.events(run_id)
    store.close()
    store = ExecutionStore(path)
    try:
        assert store.events(run_id) == original
        assert store.unresolved(run_id) == [{"operation_id": "a", "call_id": "a"}]
        assert store.recover() == [run_id]
        assert store.recent()[0]["status"] == "needs_reconciliation"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_repeated_model_cancel_cannot_interrupt_provider_cleanup(stream):
    entered, cleaning, release, stopped = (asyncio.Event() for _ in range(4))

    class Client:
        def complete_stream(self, *args, **kwargs):
            raise AssertionError("async required")

        async def acomplete_stream(self, *args, **kwargs):
            return await self.acomplete()

        async def acomplete(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                stopped.set()

    invoker = ModelInvoker(
        Client(), RuntimeConfig(model="fake", stream=stream), EventBus()
    )
    task = asyncio.create_task(
        invoker.complete([], [], session_key="s", channel="c", chat_id="1", iteration=0)
    )
    try:
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not stopped.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_admission_close_cancels_all_sessions_and_waiters_without_phantom_inputs(
    tmp_path,
):
    sessions = SessionManager(tmp_path)
    admission = TurnAdmission(sessions)
    opened = []

    async def turn(key):
        async with admission.hold(key):
            opened.append(key)
            await asyncio.Event().wait()

    tasks = [asyncio.create_task(turn(key)) for key in ("same", "same", "other")]
    try:
        await asyncio.sleep(0)
        assert opened == ["same", "other"]
        await admission.close()
        assert all(task.cancelled() for task in tasks)
        assert not admission.is_busy("same") and not admission.is_busy("other")
        assert sessions.list_sessions() == []
        with pytest.raises(RuntimeError, match="closed"):
            async with admission.hold("late"):
                pytest.fail("closed admission must reject new work")
        await admission.close()
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
        sessions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted_interrupt", [False, True])
async def test_restart_projects_interrupt_control_from_metadata_not_user_text(
    tmp_path, persisted_interrupt
):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("cli:1")
    session.add_message("user", "old task [interrupted]")
    session.add_message("assistant", "[interrupted]", interrupted=persisted_interrupt)
    sessions.save(session)
    original = list(session.messages)
    sessions.close()

    class Check:
        async def acomplete(self, messages, *args, **kwargs):
            system = "\n".join(
                str(m["content"]) for m in messages if m["role"] == "system"
            )
            assert (
                "previous turn was interrupted and is closed" in system
            ) is persisted_interrupt
            assert (
                "latest request explicitly asks to resume" in system
                if persisted_interrupt
                else True
            )
            assert "new request" in str(messages[-1]["content"])
            return ModelResponse(text="new reply")

    _, loop, reloaded, _ = build_test_runtime(tmp_path, Check())
    try:
        result = await loop.pipeline.run(
            InboundMessage("cli", "u", "1", "new request"),
            "cli:1",
            dispatch_outbound=False,
        )
        assert result.content == "new reply"
        assert reloaded.get_or_create("cli:1").messages[:2] == original
    finally:
        reloaded.close()
        await loop.pipeline.tools.shutdown()
