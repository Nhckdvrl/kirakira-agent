"""Exercise application startup rollback and physical, failure-tolerant shutdown."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bootstrap.runtime import build_runtime


async def runtime_at(path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "fixture")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "http://example.test/v1")
    (path / "config.toml").write_text("[memory]\nenabled = false\n")
    return await build_runtime(path)


def assert_sessions_closed(runtime):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        runtime.session_manager._index.execute("SELECT 1")


@pytest.mark.asyncio
async def test_channel_start_failure_rolls_back_all_background_tasks(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    failed_channels = SimpleNamespace(
        start_all=AsyncMock(side_effect=RuntimeError("channel failed")),
        stop_all=AsyncMock(),
    )
    runtime.channel_host = failed_channels
    with pytest.raises(RuntimeError, match="channel failed"):
        await runtime.start_background()
    assert failed_channels.stop_all.await_count == 1
    assert_sessions_closed(runtime)
    assert not [
        t
        for t in asyncio.all_tasks()
        if t.get_name() in {"agent_loop", "bus_dispatch", "memory_maintenance"}
        and not t.done()
    ]
    await runtime.stop_background([])
    with pytest.raises(RuntimeError, match="only be started once"):
        await runtime.start_background()


@pytest.mark.asyncio
async def test_failed_resource_does_not_skip_later_closers(tmp_path, monkeypatch):
    runtime = await runtime_at(tmp_path, monkeypatch)
    shutdown = runtime.tools.shutdown
    closed = []

    async def fail_after_disposal():
        await shutdown()
        closed.append("tools")
        raise RuntimeError("tool cleanup fixture failed")

    monkeypatch.setattr(runtime.tools, "shutdown", fail_after_disposal)
    close = runtime.control_store.close

    def record_control_close():
        close()
        closed.append("control")

    monkeypatch.setattr(runtime.control_store, "close", record_control_close)
    with pytest.raises(ExceptionGroup) as result:
        await runtime.stop_background([])
    assert "tool cleanup fixture failed" in repr(result.value)
    assert closed == ["tools", "control"]
    assert_sessions_closed(runtime)
    with pytest.raises(ExceptionGroup):
        await runtime.stop_background([])
    assert closed == ["tools", "control"]


@pytest.mark.asyncio
async def test_cancelling_close_waits_for_physical_disposal_and_shared_waiters(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    shutdown = runtime.tools.shutdown
    calls = []

    async def slow_shutdown():
        calls.append("tools")
        started.set()
        await release.wait()
        await shutdown()

    monkeypatch.setattr(runtime.tools, "shutdown", slow_shutdown)
    first = asyncio.create_task(runtime.stop_background([]))
    await started.wait()
    second = asyncio.create_task(runtime.stop_background([]))
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert calls == ["tools"]
    assert_sessions_closed(runtime)


@pytest.mark.asyncio
async def test_shutdown_stops_producers_before_closing_tools(tmp_path, monkeypatch):
    runtime = await runtime_at(tmp_path, monkeypatch)
    release = asyncio.Event()
    events = []

    async def producing():
        try:
            await release.wait()
        finally:
            await asyncio.sleep(0)
            events.append("producer stopped")

    producer = asyncio.create_task(producing(), name="scheduler")
    await asyncio.sleep(0)
    shutdown = runtime.tools.shutdown

    async def close_tools():
        assert producer.done()
        events.append("tools closed")
        await shutdown()

    monkeypatch.setattr(runtime.tools, "shutdown", close_tools)
    await runtime.stop_background([producer])
    assert events == ["producer stopped", "tools closed"]


@pytest.mark.asyncio
async def test_shutdown_drains_direct_turns_and_waiters_before_closing_stores(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    model_calls = []

    class Blocking:
        async def acomplete(self, *args, **kwargs):
            model_calls.append("started")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelling.set()
                await release.wait()
                model_calls.append("stopped")

    runtime.reasoner._model_io.model_client = Blocking()
    first = asyncio.create_task(runtime.process_direct("active", session_key="test:direct"))
    await entered.wait()
    waiter = asyncio.create_task(runtime.process_direct("queued", session_key="test:direct"))
    await asyncio.sleep(0)
    closing = asyncio.create_task(runtime.stop_background([]))
    try:
        await asyncio.wait_for(cancelling.wait(), 3)
        closing.cancel()
        await asyncio.sleep(0)
        # Even a cancelled shutdown cannot close the database before turn finally.
        assert not first.done() and not closing.done()
        runtime.session_manager._index.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="shutting down"):
            await runtime.process_direct("late")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        results = await asyncio.gather(first, waiter, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert model_calls == ["started", "stopped"]
        assert not runtime.pipeline.admission.is_busy("test:direct")
        assert_sessions_closed(runtime)
        await runtime.stop_background([])
    finally:
        release.set()
        await asyncio.gather(first, waiter, closing, return_exceptions=True)
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_one_shot_disabled_or_failed_channel_still_closes_runtime(
    tmp_path, monkeypatch
):
    from bootstrap.app import _run_proactive_once

    runtime = await runtime_at(tmp_path, monkeypatch)
    assert runtime.proactive_loop is None
    await _run_proactive_once(runtime)
    assert_sessions_closed(runtime)

    second_path = tmp_path / "second"
    second_path.mkdir()
    runtime = await runtime_at(second_path, monkeypatch)
    runtime.proactive_loop = SimpleNamespace(stop=lambda: None, close=lambda: None)
    runtime.channel_host = SimpleNamespace(
        start_all=AsyncMock(side_effect=RuntimeError("fixture start failed")),
        stop_all=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="fixture start failed"):
        await _run_proactive_once(runtime)
    assert_sessions_closed(runtime)


@pytest.mark.asyncio
async def test_runtime_owns_started_tasks_even_if_caller_passes_an_empty_list(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    tasks = await runtime.start_background(start_channels=False)
    await runtime.stop_background([])
    assert tasks and all(t.done() for t in tasks)
    with pytest.raises(RuntimeError, match="shutting down"):
        await runtime.process_direct("must not reopen closed stores")


@pytest.mark.asyncio
async def test_tool_registry_closes_all_callbacks_despite_one_failure():
    from agent.tools.registry import ToolRegistry

    tools = ToolRegistry()
    closed = []
    tools.add_shutdown_callback(lambda: closed.append("earliest"))

    def fails():
        raise RuntimeError("fixture callback failed")

    tools.add_shutdown_callback(fails)
    tools.add_shutdown_callback(lambda: closed.append("latest"))
    with pytest.raises(ExceptionGroup, match="resource cleanup"):
        await tools.shutdown()
    assert closed == ["latest", "earliest"]
    with pytest.raises(RuntimeError, match="closed"):
        tools.add_shutdown_callback(lambda: None)


@pytest.mark.asyncio
async def test_late_assembly_failure_rolls_back_earlier_resources(
    tmp_path, monkeypatch
):
    from agent.plugins import PluginManager
    from bootstrap import runtime as assembly
    from core.memory.legacy import MemoryRuntime
    from session.manager import SessionManager

    closed = []
    close_session, stop_memory, stop_plugins = (
        SessionManager.close,
        MemoryRuntime.shutdown,
        PluginManager.terminate_all,
    )

    def record_session(instance):
        close_session(instance)
        closed.append("session")

    async def record_memory(instance):
        await stop_memory(instance)
        closed.append("memory")

    async def record_plugins(instance):
        await stop_plugins(instance)
        closed.append("plugins")

    def fail(**kwargs):
        raise RuntimeError("fixture late assembly failed")

    monkeypatch.setattr(SessionManager, "close", record_session)
    monkeypatch.setattr(MemoryRuntime, "shutdown", record_memory)
    monkeypatch.setattr(PluginManager, "terminate_all", record_plugins)
    monkeypatch.setattr(assembly, "build_control_plane", fail)
    with pytest.raises(RuntimeError, match="late assembly failed"):
        await runtime_at(tmp_path, monkeypatch)
    assert closed == ["plugins", "memory", "session"]
