"""Real scheduler/session/pipeline contracts; model transport is deterministic."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agent.core.contracts import RuntimeConfig
from agent.core.execution import ExecutionStore
from agent.core.pipeline import PassiveTurnPipeline
from agent.core.reasoner import DefaultReasoner
from agent.model_runtime.types import ModelRequestError
from agent.prompting.context_builder import ContextBuilder
from agent.tools.registry import ToolRegistry
from bootstrap.tools import build_tool_registry
from bus.event_bus import EventBus
from bus.queue import MessageBus
from core.memory.legacy import MemoryRuntime
from core.schema import ModelResponse, ToolCall, ToolSpec
from plugins.scheduler.service import SchedulerService
from plugins.scheduler.store import JobStore
from session.manager import SessionManager


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def acomplete(self, messages, *args, **kwargs):
        self.calls.append(json.loads(json.dumps(messages)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest_asyncio.fixture
async def runtime(tmp_path):
    sessions = SessionManager(tmp_path)
    memory = MemoryRuntime(tmp_path, session_manager=sessions, engine="legacy")
    bus, events = (
        MessageBus(),
        EventBus(),
    )
    tools = build_tool_registry(
        tmp_path, memory=memory, session_manager=sessions, bus=bus
    )
    model = Model([ModelResponse(text="done")])
    executions = ExecutionStore(tmp_path / "executions.db")
    reasoner = DefaultReasoner(
        model_client=model,
        tools=tools,
        config=RuntimeConfig(model="fixture", stream=False),
        context=ContextBuilder(tmp_path, memory),
        event_bus=events,
        execution_store=executions,
    )
    pipeline = PassiveTurnPipeline(
        bus=bus,
        event_bus=events,
        session_manager=sessions,
        memory=memory,
        tools=tools,
        reasoner=reasoner,
        config=reasoner.config,
    )
    scheduler = SchedulerService(
        tmp_path / "schedules.json",
        bus=bus,
        tools=tools,
        pipeline=pipeline,
        sessions=sessions,
        executions=executions,
    )
    key = "web:origin"
    session = sessions.get_or_create(key)
    session.add_message("user", "HISTORY_SECRET_7429")
    session.add_message("assistant", "Recorded")
    sessions.save(session)
    tools.set_context(session_key=key, channel="web", chat_id="origin")
    received = []

    async def collect(message):
        received.append(message)

    bus.subscribe_outbound("web", collect)
    dispatch = asyncio.create_task(bus.dispatch_outbound())
    value = SimpleNamespace(
        scheduler=scheduler,
        sessions=sessions,
        memory=memory,
        bus=bus,
        tools=tools,
        model=model,
        pipeline=pipeline,
        received=received,
        executions=executions,
        path=tmp_path,
        key=key,
    )
    try:
        yield value
    finally:
        await scheduler.shutdown()
        await tools.shutdown()
        await memory.shutdown()
        await events.shutdown()
        bus.stop()
        await bus.shutdown()
        dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)
        sessions.close()
        executions.close()


async def due(runtime, **kwargs):
    created = json.loads(
        await runtime.scheduler.schedule(
            text="task input", trigger="after", when="1h", **kwargs
        )
    )

    def past(state):
        job = state.jobs[created["id"]]
        state.jobs[job.id] = job.model_copy(
            update={"run_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )

    runtime.scheduler.store.change(past)
    return created["id"]


def fired(runtime):
    return list(runtime.scheduler.store.read().fires.values())


@pytest.mark.asyncio
async def test_notify_waits_for_actual_receipt_and_uses_original_message(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(message):
        entered.set()
        await release.wait()

    runtime.bus.subscribe_outbound("web", slow)
    job = await due(runtime)
    task = asyncio.create_task(runtime.scheduler.fire(job))
    await entered.wait()
    assert fired(runtime)[0].status == "delivering"
    assert runtime.scheduler.store.read().jobs[job].status == "pending"
    assert runtime.model.calls == []
    release.set()
    await task
    fire = fired(runtime)[0]
    assert fire.status == "completed"
    assert len(runtime.received) == 1
    message = runtime.received[0]
    assert message.session_message_id == fire.output_id
    saved = runtime.sessions.get_or_create(runtime.key).messages[-1]
    assert saved["id"] == message.session_message_id and saved["source"] == fire.source


@pytest.mark.asyncio
@pytest.mark.parametrize("context", ["none", "session"])
async def test_run_uses_creating_session_and_explicit_history(runtime, context):
    job = await due(runtime, action="run", context=context)
    await runtime.scheduler.fire(job)
    payload = json.dumps(runtime.model.calls)
    assert ("HISTORY_SECRET_7429" in payload) == (context == "session")
    source = fired(runtime)[0].source
    messages = runtime.sessions.get_or_create(runtime.key).messages[-2:]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert all(m["source"] == source and m["skip_post_memory"] for m in messages)
    assert len(runtime.sessions.list_sessions()) == 1
    assert runtime.received[0].session_message_id == messages[-1]["id"]


@pytest.mark.asyncio
async def test_no_model_tools_can_redirect_the_task_to_another_session(runtime):
    created = json.loads(
        await runtime.scheduler.schedule(
            text="bound", trigger="every", when="0 9 * * *", timezone="Asia/Shanghai"
        )
    )
    assert created["session_key"] == runtime.key and created["remaining_runs"] == -1
    properties = runtime.tools.get_tool("schedule").spec.input_schema["properties"]
    assert not {"channel", "chat_id", "tier", "message", "prompt"} & set(properties)
    runtime.tools.set_context(session_key="web:other", channel="web", chat_id="other")
    assert json.loads(await runtime.scheduler.list_schedules()) == []
    assert "not found" in await runtime.scheduler.cancel_schedule(created["id"])
    runtime.tools.set_context(session_key=runtime.key, channel="web", chat_id="origin")
    assert "Cancelled" in await runtime.scheduler.cancel_schedule(created["id"])


@pytest.mark.asyncio
async def test_concurrent_fire_claims_deliver_once(runtime):
    job = await due(runtime)
    await asyncio.gather(runtime.scheduler.fire(job), runtime.scheduler.fire(job))
    await runtime.scheduler.fire(job)
    assert len(runtime.received) == 1 and len(fired(runtime)) == 1


@pytest.mark.asyncio
async def test_run_keeps_tools_in_source_session_and_disables_memory_writes(runtime):
    runtime.model.responses = [
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "write", "write_file", {"path": "result.txt", "content": "verified"}
                )
            ]
        ),
        ModelResponse(text="file complete"),
    ]
    job = await due(runtime, action="run")
    await runtime.scheduler.fire(job)
    assert (runtime.path / "result.txt").read_text() == "verified"
    saved = runtime.sessions.get_or_create(runtime.key).messages[-1]
    assert saved["tool_chain"] and fired(runtime)[0].status == "completed"
    # A provider may emit an unadvertised tool; authorization still rejects it.
    runtime.model.responses = [
        ModelResponse(
            tool_calls=[ToolCall("mutate", "memorize", {"content": "pollution"})]
        ),
        ModelResponse(text="did not write"),
    ]
    job = await due(runtime, action="run")
    await runtime.scheduler.fire(job)
    assert not runtime.memory._records


@pytest.mark.asyncio
async def test_model_failure_is_persistent_and_does_not_become_success(runtime):
    runtime.model.responses = [ModelRequestError("fixture model failed")]
    job = await due(runtime, action="run")
    await runtime.scheduler.fire(job)
    assert fired(runtime)[0].status == "failed"
    assert runtime.scheduler.store.read().jobs[job].status == "failed"
    assert not runtime.received
    assert runtime.sessions.get_or_create(runtime.key).messages[-1]["scheduler_failure"]
    await runtime.scheduler.fire(job)
    assert len(runtime.model.calls) == 1


@pytest.mark.asyncio
async def test_delivery_failure_is_not_marked_done_or_retried(runtime):
    async def fails(message):
        raise RuntimeError("fixture subscriber failed after possible send")

    runtime.bus.subscribe_outbound("web", fails)
    job = await due(runtime)
    await runtime.scheduler.fire(job)
    assert fired(runtime)[0].status == "needs_reconciliation"
    await runtime.scheduler.fire(job)
    assert len(runtime.received) == 1


@pytest.mark.asyncio
async def test_recovery_never_reexecutes_an_inflight_fire(runtime):
    job_id = await due(runtime, action="run")
    fire = runtime.scheduler._claim(job_id)
    runtime.scheduler._phase(
        fire.job.fire_key, "running", started_at=datetime.now(timezone.utc)
    )
    tools = ToolRegistry()
    reopened = SchedulerService(
        runtime.path / "schedules.json",
        bus=runtime.bus,
        tools=tools,
        pipeline=runtime.pipeline,
        sessions=runtime.sessions,
    )
    try:
        assert (
            reopened.store.read().fires[fire.job.fire_key].status
            == "needs_reconciliation"
        )
        await reopened.fire(job_id)
        assert runtime.model.calls == []
    finally:
        await reopened.shutdown()


@pytest.mark.asyncio
async def test_queued_fire_can_recover_before_any_model_or_tool_execution(runtime):
    job = await due(runtime, action="run")
    runtime.scheduler._claim(job)
    from agent.tools.registry import ToolRegistry

    reopened = SchedulerService(
        runtime.path / "schedules.json",
        bus=runtime.bus,
        tools=ToolRegistry(),
        pipeline=runtime.pipeline,
        sessions=runtime.sessions,
    )
    try:
        await reopened.fire(job)
        assert len(runtime.model.calls) == 1 and len(runtime.received) == 1
    finally:
        await reopened.shutdown()


@pytest.mark.asyncio
async def test_busy_session_queues_and_cancellation_before_admission_has_no_effect(
    runtime,
):
    job = await due(runtime, action="run")
    async with runtime.pipeline.admission.hold(runtime.key):
        task = asyncio.create_task(runtime.scheduler.fire(job))
        await asyncio.sleep(0)
        assert fired(runtime)[0].status == "pending" and not runtime.model.calls
        await runtime.scheduler.cancel_schedule(job)
        with pytest.raises(asyncio.CancelledError):
            await task
    assert fired(runtime)[0].status == "cancelled"
    assert len(runtime.sessions.get_or_create(runtime.key).messages) == 2


@pytest.mark.asyncio
async def test_cancellation_of_unknown_tool_outcome_never_replays(runtime):
    started = asyncio.Event()
    writes = []

    async def external():
        writes.append("effect")
        started.set()
        await asyncio.Event().wait()

    runtime.tools.register(
        ToolSpec("external_fixture", "fixture", {"type": "object", "properties": {}}),
        external,
        risk="external-side-effect",
    )
    runtime.model.responses = [
        ModelResponse(tool_calls=[ToolCall("external", "external_fixture", {})])
    ]
    job = await due(runtime, action="run")
    task = asyncio.create_task(runtime.scheduler.fire(job))
    await asyncio.wait_for(started.wait(), timeout=3)
    await runtime.scheduler.cancel_schedule(job)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fired(runtime)[0].status == "needs_reconciliation"
    assert writes == ["effect"] and not runtime.received
    await runtime.scheduler.fire(job)
    assert writes == ["effect"]


def test_malformed_storage_is_not_silently_discarded(tmp_path):
    path = tmp_path / "schedules.json"
    for text in ["{", '{"jobs":[]}', '{"version":2,"version":2,"jobs":{},"fires":{}}']:
        path.write_text(text)
        with pytest.raises(ValueError):
            JobStore(path)
        assert path.read_text() == text


@pytest.mark.asyncio
async def test_failed_claim_publication_prevents_model_and_delivery(
    runtime, monkeypatch
):
    job = await due(runtime, action="run")
    before = runtime.scheduler.store.path.read_bytes()

    def fail(*args):
        raise OSError("fixture disk full")

    monkeypatch.setattr("plugins.scheduler.store.atomic_write", fail)
    with pytest.raises(OSError, match="disk full"):
        await runtime.scheduler.fire(job)
    assert not runtime.model.calls and not runtime.received
    assert runtime.scheduler.store.path.read_bytes() == before
    with pytest.raises(RuntimeError, match="reopen"):
        runtime.scheduler.store.read()


@pytest.mark.asyncio
async def test_background_timer_runs_and_releases_its_task(runtime):
    job = await due(runtime)
    runner = asyncio.create_task(runtime.scheduler.run())
    try:
        async with asyncio.timeout(3):
            while not runtime.received or runtime.scheduler._tasks:
                await asyncio.sleep(0.01)
        assert runtime.scheduler.store.read().jobs[job].status == "completed"
        assert len(runtime.received) == 1
    finally:
        runtime.scheduler.stop()
        await runner


@pytest.mark.asyncio
async def test_offline_recurring_jobs_skip_backlog_and_one_shot_is_missed(runtime):
    recurring = await due(runtime)
    single = await due(runtime)
    past = datetime.now(timezone.utc) - timedelta(days=20)

    def offline(state):
        for key in (recurring, single):
            old = state.jobs[key]
            update = {"run_at": past}
            if key == recurring:
                update.update(trigger="every", interval_seconds=60, remaining_runs=-1)
            state.jobs[key] = old.model_copy(update=update)

    runtime.scheduler.store.change(offline)
    runtime.scheduler._recover()
    state = runtime.scheduler.store.read()
    assert state.jobs[single].status == "missed"
    assert state.jobs[recurring].status == "pending"
    assert state.jobs[recurring].run_at > datetime.now(timezone.utc)
    assert (state.jobs[recurring].run_at - past).total_seconds() % 60 == 0
    assert not state.fires and not runtime.received and not runtime.model.calls


@pytest.mark.asyncio
async def test_capacity_errors_and_invalid_schedules_leave_existing_jobs(runtime):
    for when in ("0s", "bad"):
        assert "Error:" in await runtime.scheduler.schedule("task", "every", when)
    assert "Error:" in await runtime.scheduler.schedule(
        "task", "at", "tomorrow", timezone="invalid/zone"
    )
    for _ in range(runtime.scheduler.MAX_ACTIVE_JOBS):
        assert "job_" in await runtime.scheduler.schedule("task", "after", "1h")
    before = runtime.scheduler.store.path.read_bytes()
    assert "capacity" in await runtime.scheduler.schedule("task", "after", "1h")
    assert runtime.scheduler.store.path.read_bytes() == before
