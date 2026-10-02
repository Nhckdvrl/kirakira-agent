"""Execution facts, cancellation and admission contracts; no live providers."""

import asyncio

import pytest

from agent.core.admission import TurnAdmission
from agent.core.contracts import RuntimeConfig
from agent.core.execution import ExecutionStore
from agent.core.model_io import ModelInvoker
from agent.core.reasoner import DefaultReasoner
from agent.core.scoped import run_scoped
from agent.model_runtime.types import ContextLengthError
from agent.tools.registry import ToolRegistry
from agent.turns.policy import ReasoningPolicy
from agent.turns.result import TurnOutbound, TurnResult, commit_turn_result
from bus.event_bus import EventBus
from bus.events import InboundMessage
from core.schema import ModelResponse, ToolCall, ToolSpec
from session.manager import SessionManager
from tests.test_runtime import build_test_runtime
from tests.test_turn_result import _effect, _Port


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    async def acomplete(self, *args, **kwargs):
        self.calls += 1
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def reasoner(tmp_path, model, tools=None):
    return DefaultReasoner(
        model_client=model,
        tools=tools or ToolRegistry(),
        config=RuntimeConfig(model="fake", stream=False),
        context=None,
        event_bus=EventBus(),
        execution_store=ExecutionStore(tmp_path / "executions.db"),
    )


async def run(engine, **kwargs):
    return await engine.run(
        [{"role": "user", "content": "work"}],
        session_key="test:1",
        channel="test",
        chat_id="1",
        request_text="work",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_journal_records_tool_intent_before_side_effect(tmp_path):
    tools = ToolRegistry()
    model = Model(
        ModelResponse(tool_calls=[ToolCall("c", "write", {})]),
        ModelResponse(text="done"),
    )
    engine = reasoner(tmp_path, model, tools)

    def write():
        row = engine.execution_store.recent()[0]
        assert engine.execution_store.events(row["run_id"])[-1]["kind"] == "tool_intent"
        return "written"

    tools.register(ToolSpec("write", "write", {}), write)
    result = await run(engine)
    store = engine.execution_store
    assert [e["kind"] for e in store.events(result.execution_id)] == [
        "model_request",
        "model_response",
        "tool_intent",
        "tool_result",
        "model_request",
        "model_response",
    ]
    assert store.recent()[0]["status"] == "completed"
    assert store.unresolved(result.execution_id) == []
    store.close()


def test_recovery_never_replays_uncertain_tools(tmp_path):
    path = tmp_path / "executions.db"
    store = ExecutionStore(path)
    uncertain = store.start(session_key="a", scope="test", parent_id=None, inputs={})
    clean = store.start(session_key="b", scope="test", parent_id=None, inputs={})
    store.append(uncertain, "tool_intent", {"operation_id": "op", "name": "send"})
    store.close()
    store = ExecutionStore(path)
    assert set(store.recover()) == {uncertain, clean}
    assert {r["run_id"]: r["status"] for r in store.recent()} == {
        uncertain: "needs_reconciliation",
        clean: "interrupted",
    }
    assert store.recover() == []
    store.close()
    readonly = ExecutionStore(path, read_only=True)
    assert readonly.unresolved(uncertain)[0]["name"] == "send"
    readonly.close()


@pytest.mark.asyncio
async def test_cancelled_tool_requires_reconciliation(tmp_path):
    entered = asyncio.Event()
    tools = ToolRegistry()

    async def send():
        entered.set()
        await asyncio.Event().wait()

    tools.register(ToolSpec("send", "send", {}), send)
    engine = reasoner(
        tmp_path, Model(ModelResponse(tool_calls=[ToolCall("c", "send", {})])), tools
    )
    task = asyncio.create_task(run(engine))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.execution_store.recent()[0]["status"] == "needs_reconciliation"
    engine.execution_store.close()


@pytest.mark.asyncio
async def test_model_rejection_after_tool_does_not_retry_conversation(tmp_path):
    model = Model(
        ModelResponse(
            tool_calls=[ToolCall("c", "write_file", {"path": "once", "content": "x"})]
        ),
        ContextLengthError("too long"),
        ModelResponse(text="must not retry"),
    )
    _, loop, sessions, _ = build_test_runtime(tmp_path, model)
    engine = loop.pipeline.reasoner
    engine.execution_store = ExecutionStore(tmp_path / "executions.db")
    with pytest.raises(RuntimeError, match="refusing to replay"):
        await loop.pipeline.run(
            InboundMessage("cli", "user", "1", "write"),
            "cli:1",
            dispatch_outbound=False,
        )
    assert model.calls == 2
    assert (tmp_path / "once").read_text() == "x"
    assert engine.execution_store.recent()[0]["status"] == "failed"
    engine.execution_store.close()
    sessions.close()


@pytest.mark.asyncio
async def test_stream_cancel_joins_provider_worker():
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Stream:
        async def acomplete_stream(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        def complete_stream(self, *args):
            raise AssertionError("must use async stream")

    invoker = ModelInvoker(Stream(), RuntimeConfig(model="fake"), EventBus())
    task = asyncio.create_task(
        invoker.complete([], [], session_key="s", channel="c", chat_id="1", iteration=0)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_pipeline_serializes_direct_calls_and_interrupts_active_turn(tmp_path):
    entered = asyncio.Event()

    class Blocking:
        async def acomplete(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    _, loop, sessions, _ = build_test_runtime(tmp_path, Blocking())
    first = asyncio.create_task(
        loop.pipeline.run(
            InboundMessage("cli", "u", "1", "first"), "cli:1", dispatch_outbound=False
        )
    )
    await entered.wait()
    assert loop.is_busy("cli:1")
    assert loop.request_interrupt("cli:1")
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not loop.is_busy("cli:1")
    messages = sessions.get_or_create("cli:1").messages
    assert any(m.get("content") == "first" for m in messages)
    sessions.close()


@pytest.mark.asyncio
async def test_admission_waiter_cancellation_does_not_release_owner(tmp_path):
    sessions = SessionManager(tmp_path)
    admission = TurnAdmission(sessions)
    async with admission.hold("s"):

        async def wait():
            async with admission.hold("s"):
                pytest.fail("must not enter")

        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert admission.is_busy("s")
        async with admission.hold("other"):
            assert admission.is_busy("other")
    assert not admission.is_busy("s")
    async with admission.hold("s"):
        pass
    sessions.close()


@pytest.mark.asyncio
async def test_scoped_program_uses_durable_executor(tmp_path):
    store = ExecutionStore(tmp_path / "executions.db")
    result = await run_scoped(
        model_client=Model(ModelResponse(text="decision")),
        registry=ToolRegistry(),
        model="fake",
        system="decide",
        prompt="event",
        session_key="wake:1",
        max_steps=2,
        max_tokens=100,
        policy=ReasoningPolicy(),
        channel="wake",
        execution_store=store,
    )
    assert store.recent()[0]["run_id"] == result.execution_id
    assert store.recent()[0]["scope"] == "wake"
    store.close()


@pytest.mark.asyncio
async def test_failed_send_precondition_prevents_dispatch():
    port, log = _Port(), []
    outcome = await commit_turn_result(
        TurnResult(
            decision="reply",
            outbound=TurnOutbound(session_key="s", content="message"),
            side_effects=[_effect("durable-intent", log, boom=True)],
            failure_side_effects=[_effect("rollback", log)],
        ),
        port=port,
    )
    assert not outcome.dispatched
    assert port.calls == []
    assert log == []


@pytest.mark.asyncio
async def test_intent_persistence_failure_prevents_tool_and_stops_run(tmp_path):
    tools, effects = ToolRegistry(), []
    tools.register(ToolSpec("send", "send", {}), lambda: effects.append("sent"))
    model = Model(
        ModelResponse(tool_calls=[ToolCall("c", "send", {})]),
        ModelResponse(text="must not continue"),
    )
    engine = reasoner(tmp_path, model, tools)
    store = engine.execution_store
    append = store.append

    def fail_intent(run_id, kind, payload):
        if kind == "tool_intent":
            raise OSError("intent not durable")
        append(run_id, kind, payload)

    store.append = fail_intent
    with pytest.raises(OSError, match="not durable"):
        await run(engine)
    assert effects == []
    assert model.calls == 1
    assert store.recent()[0]["status"] == "failed"
    store.close()


@pytest.mark.asyncio
async def test_hook_cannot_use_revoked_grant(tmp_path):
    from agent.tool_hooks import HookOutcome

    tools, effects, grant = ToolRegistry(), [], {"send"}
    tools.register(ToolSpec("send", "send", {}), lambda: effects.append("sent"))
    engine = reasoner(
        tmp_path,
        Model(
            ModelResponse(tool_calls=[ToolCall("c", "send", {})]),
            ModelResponse(text="done"),
        ),
        tools,
    )

    class Revoke:
        name, event = "revoke", "pre_tool_use"

        def matches(self, ctx):
            return True

        async def run(self, ctx):
            grant.clear()
            return HookOutcome()

    engine.add_tool_hooks([Revoke()])
    result = await run(
        engine, policy=ReasoningPolicy(current_tools=lambda: frozenset(grant))
    )
    assert effects == []
    assert not any(
        e["kind"] == "tool_intent"
        for e in engine.execution_store.events(result.execution_id)
    )
    engine.execution_store.close()


@pytest.mark.asyncio
async def test_two_direct_calls_serialize_while_other_session_progresses(tmp_path):
    entered, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Pausing:
        calls = 0

        async def acomplete(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
            else:
                other_done.set()
            return ModelResponse(text="done")

    model = Pausing()
    _, loop, sessions, _ = build_test_runtime(tmp_path, model)

    async def turn(key, text):
        return await loop.pipeline.run(
            InboundMessage("cli", "u", key, text), "cli:" + key, dispatch_outbound=False
        )

    first = asyncio.create_task(turn("1", "first"))
    await entered.wait()
    second = asyncio.create_task(turn("1", "second"))
    other = asyncio.create_task(turn("2", "other"))
    await asyncio.wait_for(other_done.wait(), 5)
    assert not second.done()
    assert model.calls == 2
    release.set()
    await asyncio.gather(first, second, other)
    assert [
        m["content"]
        for m in sessions.get_or_create("cli:1").messages
        if m["role"] == "user"
    ] == ["first", "second"]
    sessions.close()
