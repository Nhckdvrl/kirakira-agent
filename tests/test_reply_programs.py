"""Production reply-program tests over real SQLite and actual tool effects."""

import asyncio
import threading
import json

import pytest

from bus.events_lifecycle import AfterReasoningCtx
from core.schema import ModelResponse, ToolCall, ToolSpec
from plugins.control.client import ControlClient
from plugins.ledger.owner_store import StateConflict
from plugins.models.types import ContextLengthError
from tests.test_workspace_features import runtime_at


def publish_job(runtime, identity="job-one", key="test:jobs", result="RESULT_713"):
    runtime.session_manager.get_or_create(key)
    return runtime.pipeline.jobs.publish(
        identity,
        session_key=key,
        channel="test",
        chat_id="jobs",
        label="verification",
        profile="research",
        result={"status": "completed", "result": result},
    )


@pytest.mark.asyncio
async def test_background_result_joins_current_program_and_suppresses_queued_wake(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)

    def settle():
        publish_job(runtime)
        return "background result available"

    runtime.tools.register(ToolSpec("settle", "settle", {"type": "object"}), settle)

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(tool_calls=[ToolCall("settle-1", "settle", {})])
            assert "RESULT_713" in json.dumps(messages)
            return ModelResponse(text="incorporated RESULT_713")

    client = Model()
    model(runtime, client)
    try:
        reply = await runtime.process_direct("start", session_key="test:jobs")
        notice = runtime.pipeline.jobs.read("job-one")
        assert notice["status"] == "consumed"
        assert notice["program_id"] == reply.metadata["program_id"]
        assert notice["message_id"] == reply.session_message_id
        assert (
            await runtime.pipeline.run(None, "test:jobs", job_notice_id="job-one")
            is None
        )
        assert client.calls == 2
        assert len(runtime.pipeline.programs.store.list("test:jobs")) == 1
        assert [
            m["role"]
            for m in runtime.session_manager.get_or_create("test:jobs").messages
        ] == ["user", "assistant"]
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_pending_job_survives_restart_and_idle_wakes_without_synthetic_user(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    publish_job(runtime)
    request = runtime.pipeline.jobs.wake_request("job-one", "test:jobs")
    accepted_id, _ = runtime.pipeline.programs.accept(request, "test:jobs")
    await runtime.stop_background([])
    runtime = await runtime_at(tmp_path, monkeypatch)
    delivered = asyncio.Event()
    replies = []

    class Model:
        async def acomplete(self, messages, *args, **kwargs):
            assert "RESULT_713" in json.dumps(messages)
            return ModelResponse(text="background RESULT_713")

    async def deliver(message):
        replies.append(message)
        delivered.set()

    model(runtime, Model())
    runtime.bus.subscribe_outbound("test", deliver)
    try:
        tasks = await runtime.start_background(start_channels=False)
        await asyncio.wait_for(delivered.wait(), 10)
        assert len(replies) == 1
        assert runtime.pipeline.jobs.read("job-one")["status"] == "consumed"
        assert runtime.pipeline.jobs.read("job-one")["program_id"] == accepted_id
        messages = runtime.session_manager.get_or_create("test:jobs").messages
        assert all(message["role"] == "assistant" for message in messages)
        assert sum(not message.get("interrupted") for message in messages) == 1
        assert messages[-1]["source"] == "job:job-one"
        assert messages[-1]["skip_post_memory"] is True
        assert (
            await runtime.pipeline.run(None, "test:jobs", job_notice_id="job-one")
            is None
        )
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_claimed_job_replays_after_cancel_and_restart_without_consuming_early(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    publish_job(runtime)
    entered = asyncio.Event()

    class Blocked:
        async def acomplete(self, messages, *args, **kwargs):
            assert "RESULT_713" in json.dumps(messages)
            entered.set()
            await asyncio.Event().wait()

    model(runtime, Blocked())
    task = asyncio.create_task(
        runtime.process_direct("continue", session_key="test:jobs")
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        identity = runtime.pipeline.jobs.read("job-one")["program_id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.pipeline.jobs.read("job-one")["status"] == "claimed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.stop_background([])
    runtime = await runtime_at(tmp_path, monkeypatch)

    class Resumed:
        async def acomplete(self, messages, *args, **kwargs):
            assert "RESULT_713" in json.dumps(messages)
            return ModelResponse(text="recovered RESULT_713")

    model(runtime, Resumed())
    try:
        assert (
            await runtime.pipeline.run(None, "test:jobs", job_notice_id="job-one")
            is None
        )
        reply = await runtime.process_direct(
            "", session_key="test:jobs", resume_program_id=identity
        )
        assert reply.metadata["program_id"] == identity
        assert runtime.pipeline.jobs.read("job-one")["status"] == "consumed"
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_job_receipt_is_immutable_scoped_and_deleted_with_session(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    try:
        assert publish_job(runtime)
        assert not publish_job(runtime)
        with pytest.raises(StateConflict, match="identity reused"):
            publish_job(runtime, result="changed")
        with pytest.raises(ValueError, match="does not belong"):
            runtime.pipeline.jobs.read("job-one", session_key="other")
        assert runtime.pipeline.jobs.claim("other", "pr_other", "conversation") == []
        assert (
            runtime.pipeline.jobs.claim("test:jobs", "pr_timer", "scheduler:one") == []
        )
        assert runtime.session_manager.delete_session("test:jobs")
        assert runtime.pipeline.jobs.list("test:jobs") == []
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_job_consumption_rolls_back_with_reply_commit(tmp_path, monkeypatch):
    runtime = await runtime_at(tmp_path, monkeypatch)
    publish_job(runtime)
    jobs = runtime.pipeline.jobs
    finish = jobs.finish

    class Model:
        async def acomplete(self, messages, *args, **kwargs):
            assert "RESULT_713" in json.dumps(messages)
            return ModelResponse(text="RESULT_713")

    def fail_commit(program_id, message_id, tx):
        finish(program_id, message_id, tx)
        raise RuntimeError("injected after job consumption")

    model(runtime, Model())
    jobs.finish = fail_commit
    try:
        with pytest.raises(RuntimeError, match="after job consumption"):
            await runtime.process_direct("start", session_key="test:jobs")
        notice = jobs.read("job-one")
        assert notice["status"] == "claimed" and notice["message_id"] is None
        identity = notice["program_id"]
        assert runtime.pipeline.programs.store.read(identity)["status"] == "failed"
        jobs.finish = finish
        reply = await runtime.process_direct(
            "", session_key="test:jobs", resume_program_id=identity
        )
        assert jobs.read("job-one")["message_id"] == reply.session_message_id
    finally:
        jobs.finish = finish
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_rejected_model_context_does_not_consume_background_result(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    publish_job(runtime)

    class Rejected:
        async def acomplete(self, *args, **kwargs):
            raise ContextLengthError("fixture context rejected")

    model(runtime, Rejected())
    try:
        await runtime.process_direct("start", session_key="test:jobs")
        assert runtime.pipeline.jobs.read("job-one")["status"] == "claimed"
        assert runtime.pipeline.jobs.read("job-one")["message_id"] is None
    finally:
        await runtime.stop_background([])


def model(runtime, value):
    runtime.reasoner._model_io.model_client = value


@pytest.mark.asyncio
async def test_jobs_owner_can_be_replaced_without_recreating_ledger_or_losing_result(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    publish_job(runtime)
    prior_jobs, sessions = runtime.pipeline.jobs, runtime.session_manager
    assert runtime.subagents.jobs is prior_jobs
    try:
        await runtime.composition.replace("jobs", runtime.composition._modules["jobs"])
        assert runtime.session_manager is sessions
        assert runtime.pipeline.jobs is not prior_jobs
        assert runtime.subagents.jobs is runtime.pipeline.jobs
        # A retired activation's late cleanup cannot remove the new registration.
        prior_jobs.close()
        with pytest.raises(ValueError, match="already registered"):
            sessions.register_initializer("job_notices", lambda session, tx: None)

        class Model:
            async def acomplete(self, messages, *args, **kwargs):
                assert "RESULT_713" in json.dumps(messages)
                return ModelResponse(text="RESULT_713")

        model(runtime, Model())
        await runtime.process_direct("report result", session_key="test:jobs")
        assert runtime.pipeline.jobs.read("job-one")["status"] == "consumed"
    finally:
        await runtime.stop_background([])


async def paused_write(root, monkeypatch):
    runtime = await runtime_at(root, monkeypatch)
    entered = asyncio.Event()

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            "write-once",
                            "bash",
                            {"command": "printf x >> once.txt", "login": False},
                        )
                    ]
                )
            entered.set()
            await asyncio.Event().wait()

    model(runtime, Model())
    task = asyncio.create_task(
        runtime.process_direct(
            "write once and explain",
            session_key="test:program",
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
    )
    await asyncio.wait_for(entered.wait(), 10)
    identity = runtime.pipeline.programs.store.list("test:program")[0]["id"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return runtime, identity


@pytest.mark.asyncio
async def test_input_is_durable_before_generation_and_duplicate_result_is_a_read(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            row = runtime.pipeline.programs.store.list("test:receipt")[0]
            assert row["request"]["content"] == "immutable input"
            assert row["status"] == "running"
            return ModelResponse(text="saved result")

    client = Model()
    model(runtime, client)
    try:
        first = await runtime.process_direct(
            "immutable input",
            session_key="test:receipt",
            request_id="stable-request",
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
        second = await runtime.process_direct(
            "immutable input",
            session_key="test:receipt",
            request_id="stable-request",
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
        assert client.calls == 1
        assert first.session_message_id == second.session_message_id
        assert first.metadata["program_id"] == second.metadata["program_id"]
        with pytest.raises(StateConflict, match="identity reused"):
            await runtime.process_direct(
                "different input",
                session_key="test:receipt",
                request_id="stable-request",
                skip_memory_retrieval=True,
                skip_post_memory=True,
            )
        assert len(runtime.session_manager.get_or_create("test:receipt").messages) == 2
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [0, 100_000])
async def test_resume_after_restart_restores_results_without_executing_tool_again(
    tmp_path, monkeypatch, window
):
    runtime, identity = await paused_write(tmp_path, monkeypatch)
    assert runtime.pipeline.programs.store.read(identity)["status"] == "paused"
    assert (tmp_path / "once.txt").read_text() == "x"
    await runtime.stop_background([])
    runtime = await runtime_at(tmp_path, monkeypatch)
    runtime.reasoner.config.context_window = window
    captured = []

    class Model:
        async def acomplete(self, messages, *args, **kwargs):
            captured.append(messages)
            calls = [c for m in messages for c in m.get("tool_calls", [])]
            assert len([c for c in calls if c["id"] == "write-once"]) == 1
            return ModelResponse(text="resumed from the existing write")

    model(runtime, Model())
    try:
        result = await runtime.process_direct(
            "ignored transport text",
            session_key="test:program",
            resume_program_id=identity,
        )
        assert (
            result.content == "resumed from the existing write" and len(captured) == 1
        )
        assert (tmp_path / "once.txt").read_text() == "x"
        assert runtime.pipeline.programs.store.read(identity)["status"] == "completed"
        history = runtime.session_manager.get_or_create("test:program").get_history()
        assert (
            sum(
                c["id"] == "write-once"
                for m in history
                for c in m.get("tool_calls", [])
            )
            == 1
        )
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_new_input_cancels_model_drains_then_reads_previous_closed_tools(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    waiting, drained = asyncio.Event(), asyncio.Event()

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            "first-effect",
                            "write_file",
                            {"path": "first.txt", "content": "kept"},
                        )
                    ]
                )
            if self.calls == 2:
                waiting.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    drained.set()
            assert drained.is_set()
            assert any(
                m.get("role") == "tool" and "first-effect" == m.get("tool_call_id")
                for m in messages
            )
            assert "new direction" in str(messages)
            return ModelResponse(text="latest input handled")

    model(runtime, Model())
    first = asyncio.create_task(
        runtime.process_direct(
            "original direction",
            session_key="test:input",
            supersede=True,
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 10)
        second = await runtime.process_direct(
            "new direction",
            session_key="test:input",
            supersede=True,
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
        with pytest.raises(asyncio.CancelledError):
            await first
        assert second.content == "latest input handled"
        rows = runtime.pipeline.programs.store.list("test:input")
        assert {r["status"] for r in rows} == {"completed", "superseded"}
        assert (tmp_path / "first.txt").read_text() == "kept"
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_new_input_during_sync_effect_waits_and_blocks_remaining_old_calls(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    started, release, ended = threading.Event(), threading.Event(), threading.Event()

    def effect():
        started.set()
        assert release.wait(10)
        (tmp_path / "physical.txt").write_text("settled")
        ended.set()
        return "settled"

    runtime.tools.register(
        ToolSpec("slow_effect", "physical effect", {}), effect, risk="write"
    )

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=[
                        ToolCall("slow", "slow_effect", {}),
                        ToolCall(
                            "must-not-run",
                            "write_file",
                            {"path": "forbidden.txt", "content": "bad"},
                        ),
                    ]
                )
            assert ended.is_set()
            assert not (tmp_path / "forbidden.txt").exists()
            return ModelResponse(text="new input after physical settlement")

    model(runtime, Model())
    first = asyncio.create_task(
        runtime.process_direct(
            "old batch",
            session_key="test:drain",
            supersede=True,
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
    )
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 10)
        second = asyncio.create_task(
            runtime.process_direct(
                "cancel remaining old work",
                session_key="test:drain",
                supersede=True,
                skip_memory_retrieval=True,
                skip_post_memory=True,
            )
        )
        await asyncio.sleep(0.02)
        assert not second.done() and not ended.is_set()
        release.set()
        result = await second
        with pytest.raises(asyncio.CancelledError):
            await first
        assert result.content == "new input after physical settlement"
        old = runtime.pipeline.programs.store.list("test:drain")[-1]
        assert old["status"] == "superseded"
        assert all(row.get("result") is not None for row in old["effects"].values())
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(
            first, *([second] if second else []), return_exceptions=True
        )
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_stale_final_transaction_rolls_back_cached_assistant_before_next_input(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    accepted = asyncio.Event()
    pending = []

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            return ModelResponse(
                text="stale final" if self.calls == 1 else "current final"
            )

    model(runtime, Model())

    async def redirect(ctx):
        if ctx.reply == "stale final":

            async def next_input():
                accepted.set()
                return await runtime.process_direct(
                    "replacement input",
                    session_key="test:commit",
                    supersede=True,
                    skip_memory_retrieval=True,
                    skip_post_memory=True,
                )

            pending.append(asyncio.create_task(next_input()))
            await accepted.wait()
            await asyncio.sleep(0)

    runtime.event_bus.on(AfterReasoningCtx, redirect)
    try:
        with pytest.raises(asyncio.CancelledError):
            await runtime.process_direct(
                "first input",
                session_key="test:commit",
                supersede=True,
                skip_memory_retrieval=True,
                skip_post_memory=True,
            )
        assert (await pending[0]).content == "current final"
        rows = runtime.session_manager.get_or_create("test:commit").messages
        assert not any(m["content"] == "stale final" for m in rows)
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_changed_history_or_tool_grant_rejects_resume_before_new_effect(
    tmp_path, monkeypatch
):
    runtime, identity = await paused_write(tmp_path, monkeypatch)

    class Model:
        async def acomplete(self, *args, **kwargs):
            raise AssertionError("changed grant must reject before generation")

    model(runtime, Model())
    runtime.tools.unregister("write_file")
    try:
        with pytest.raises(StateConflict, match="grant changed"):
            await runtime.process_direct(
                "", session_key="test:program", resume_program_id=identity
            )
        assert (tmp_path / "once.txt").read_text() == "x"
        session = runtime.session_manager.get_or_create("test:program")
        session.add_message("user", "a newer request")
        runtime.session_manager.save(session)
        with pytest.raises(StateConflict, match="history changed"):
            await runtime.process_direct(
                "", session_key="test:program", resume_program_id=identity
            )
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_unknown_effect_is_not_replayed_and_operator_resolution_has_immutable_receipt(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    entered = asyncio.Event()

    async def uncertain():
        (tmp_path / "external.txt").write_text("already happened")
        entered.set()
        await asyncio.Event().wait()

    runtime.tools.register(
        ToolSpec("uncertain", "external effect", {}),
        uncertain,
        risk="external-side-effect",
    )

    class Model:
        async def acomplete(self, *args, **kwargs):
            return ModelResponse(
                tool_calls=[ToolCall("uncertain-call", "uncertain", {})]
            )

    model(runtime, Model())
    task = asyncio.create_task(
        runtime.process_direct(
            "external action",
            session_key="test:unknown",
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store = runtime.pipeline.programs.store
        row = store.list("test:unknown")[0]
        assert row["status"] == "needs_reconciliation"
        with pytest.raises(StateConflict):
            await runtime.process_direct(
                "", session_key="test:unknown", resume_program_id=row["id"]
            )
        effect = next(iter(row["effects"]))
        resolved = store.resolve(
            row["id"],
            "test:unknown",
            effect,
            "success",
            "already happened",
            "Verified external.txt contents",
            row["revision"],
        )
        assert resolved["status"] == "paused"
        assert (
            store.resolve(
                row["id"],
                "test:unknown",
                effect,
                "success",
                "already happened",
                "Verified external.txt contents",
                row["revision"],
            )["revision"]
            == resolved["revision"]
        )
        with pytest.raises(StateConflict, match="receipt cannot be changed"):
            store.resolve(
                row["id"],
                "test:unknown",
                effect,
                "not_executed",
                "",
                "a conflicting account",
                resolved["revision"],
            )

        class ResumeModel:
            async def acomplete(self, messages, *args, **kwargs):
                assert "Operator-verified outcome" in str(messages)
                return ModelResponse(text="resumed with verified outcome")

        model(runtime, ResumeModel())
        result = await runtime.process_direct(
            "", session_key="test:unknown", resume_program_id=row["id"]
        )
        assert result.content == "resumed with verified outcome"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_final_program_and_session_commit_roll_back_together(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    store = runtime.pipeline.programs.store
    original = store.settle

    def fail_after_owner_write(*args, **kwargs):
        original(*args, **kwargs)
        if kwargs.get("result") is not None and kwargs.get("transaction") is not None:
            raise OSError("injected commit fault")

    monkeypatch.setattr(store, "settle", fail_after_owner_write)

    class Model:
        async def acomplete(self, *args, **kwargs):
            return ModelResponse(text="must not be committed")

    model(runtime, Model())
    try:
        with pytest.raises(OSError, match="injected commit fault"):
            await runtime.process_direct(
                "question",
                session_key="test:atomic",
                skip_memory_retrieval=True,
                skip_post_memory=True,
            )
        row = store.list("test:atomic")[0]
        assert row["status"] == "failed" and row["result"] is None
        session = runtime.session_manager.get_or_create("test:atomic")
        assert all(m["content"] != "must not be committed" for m in session.messages)
        assert session.messages[-1]["interrupted"] is True
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_socket_resume_and_cross_session_read_use_real_program_owner(
    tmp_path, monkeypatch
):
    runtime, identity = await paused_write(tmp_path, monkeypatch)
    await runtime.control_server.start()
    client = await ControlClient.connect(str(runtime.control_server.endpoint))

    class Model:
        async def acomplete(self, messages, *args, **kwargs):
            return ModelResponse(text="socket resumed")

    model(runtime, Model())
    try:
        row = await client.request(
            "program/read", {"threadId": "test:program", "programId": identity}
        )
        assert row["status"] == "paused"
        handle = await client.resume_program("test:program", identity)
        result = await handle.result()
        assert result["status"] == "completed", result
        assert (tmp_path / "once.txt").read_text() == "x"
        assert (
            await client.request(
                "program/read", {"threadId": "test:program", "programId": identity}
            )
        )["status"] == "completed"
    finally:
        await client.close()
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_queued_request_survives_restart_without_automatic_model_or_tool_calls(
    tmp_path, monkeypatch
):
    from bus.events import InboundMessage

    runtime = await runtime_at(tmp_path, monkeypatch)
    identity, _ = runtime.pipeline.programs.accept(
        InboundMessage("cli", "user", "queue", "retained queued input"), "test:queue"
    )
    await runtime.stop_background([])
    runtime = await runtime_at(tmp_path, monkeypatch)
    try:
        row = runtime.pipeline.programs.store.read(identity)
        assert row["status"] == "paused"
        assert row["request"]["content"] == "retained queued input"
        assert runtime.reasoner.execution_store.recent() == []
        assert (
            runtime.session_manager.get_or_create("test:queue").messages[0]["content"]
            == "retained queued input"
        )
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_deleting_active_session_is_rejected_and_completed_program_rows_are_removed(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path, monkeypatch)
    entered = asyncio.Event()

    class Model:
        async def acomplete(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    model(runtime, Model())
    task = asyncio.create_task(
        runtime.process_direct(
            "active",
            session_key="test:delete",
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        session = runtime.session_manager.get_or_create("test:delete")
        with pytest.raises(ValueError, match="stop the active"):
            runtime.session_manager.delete_session("test:delete")
        assert runtime.session_manager.get_or_create("test:delete") is session
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.session_manager.delete_session("test:delete")
        assert runtime.pipeline.programs.store.list("test:delete") == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.stop_background([])
