"""Production reply-program tests over real SQLite and actual tool effects."""

import asyncio
import json
import threading

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


@pytest.mark.asyncio
async def test_message_facts_drive_recovery_and_turn_projection(tmp_path, monkeypatch):
    from plugins.ledger.message import ContentPart, Input, Output, ToolResult
    from plugins.reply_program.store import OWNER
    from plugins.turn_projection.plugin import TurnProjection

    runtime = await runtime_at(tmp_path, monkeypatch)
    runtime.tools.register(
        ToolSpec("fact_read", "read", {"type": "object"}), lambda: "immutable-result"
    )

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            return (
                ModelResponse(tool_calls=[ToolCall("fact-1", "fact_read", {})])
                if self.calls == 1
                else ModelResponse(text="recorded")
            )

    model(runtime, Model())
    try:
        reply = await runtime.process_direct("read", session_key="test:journal")
        identity = reply.metadata["program_id"]
        store = runtime.pipeline.programs.store
        raw = store.store.read(OWNER, "program:" + identity).value
        assert "request" not in raw and "result" not in raw
        assert "text" not in raw["batches"][0]
        assert "result" not in next(iter(raw["effects"].values()))
        messages = runtime.session_manager.message_log.read("test:journal")
        assert [type(message.body) for message in messages] == [
            Input,
            Output,
            ToolResult,
            Output,
        ]
        assert messages[-1].message_id == reply.session_message_id
        ledger = runtime.session_manager.message_log
        with pytest.raises(TypeError):
            messages[0].body.parts[0].value["sender"] = "changed"
        before_head = ledger.head("test:journal")
        with pytest.raises(StateConflict, match="already has"):
            ledger.append("test:journal", "conversation", "tool", messages[2].body)
        with pytest.raises(StateConflict, match="this source"):
            ledger.append("other:session", "conversation", "tool", messages[2].body)
        with pytest.raises(RuntimeError, match="rollback"):
            with ledger.store.transaction() as tx:
                ledger.append(
                    "test:journal",
                    "conversation",
                    "assistant",
                    Output((ContentPart("text", "not committed"),), "continue"),
                    transaction=tx,
                )
                raise RuntimeError("rollback")
        assert ledger.head("test:journal") == before_head
        turns = TurnProjection().project(messages, "conversation")
        assert len(turns) == 1 and turns[0].status == "complete"
        assert turns[0].observations == (
            (messages[2].body.call_ref, messages[2].message_id),
        )
        assert (
            store.read(identity)["effects"][identity + ":0:0"]["result"]["content"]
            == "immutable-result"
        )
        assert runtime.session_manager.delete_messages([reply.session_message_id]) == 1
        assert runtime.session_manager.message_log.read("test:journal") == ()
        with pytest.raises(ValueError, match="does not belong"):
            store.read(identity)
        runtime.session_manager.delete_session("test:journal")
        assert runtime.session_manager.message_log.read("test:journal") == ()
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_steer_accepts_at_next_step_withdraws_and_keeps_one_program(
    tmp_path, monkeypatch
):
    from bus.events import InboundMessage
    from plugins.ledger.message import Input
    from plugins.turn_projection.plugin import TurnProjection

    runtime = await runtime_at(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def gate():
        effects.append("ran")
        entered.set()
        await release.wait()
        return "finished"

    runtime.tools.register(ToolSpec("steer_gate", "gate", {"type": "object"}), gate)

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(tool_calls=[ToolCall("gate-1", "steer_gate", {})])
            assert [m["content"] for m in messages if m["role"] == "user"][-1].endswith(
                "Use blue"
            )
            assert "withdraw me" not in json.dumps(messages)
            return ModelResponse(text="blue")

    model(runtime, Model())
    task = asyncio.create_task(
        runtime.process_direct("choose", session_key="test:steer")
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)

        def message(identity, content):
            return InboundMessage(
                channel="test",
                sender="human",
                chat_id="steer",
                content=content,
                metadata={"client_request_id": identity},
            )

        assert (
            runtime.pipeline.stage(message("drop", "withdraw me"), "test:steer")
            == "drop"
        )
        assert runtime.pipeline.withdraw("test:steer", "drop")
        assert (
            runtime.pipeline.stage(message("blue", "Use blue"), "test:steer") == "blue"
        )
        assert len(runtime.session_manager.message_log.read("test:steer")) == 2
        release.set()
        reply = await task
        assert effects == ["ran"]
        programs = runtime.pipeline.programs.store.list("test:steer")
        assert len(programs) == 1 and len(programs[0]["steer_ids"]) == 1
        assert not runtime.pipeline.withdraw("test:steer", "blue")
        messages = runtime.session_manager.message_log.read("test:steer")
        assert sum(isinstance(message.body, Input) for message in messages) == 2
        turns = TurnProjection().project(messages, "conversation")
        assert len(turns) == 1 and turns[0].status == "complete"
        assert reply.content == "blue"
        assert [
            m["content"]
            for m in runtime.session_manager.get_or_create("test:steer").messages
        ] == ["choose", "Use blue", "blue"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.stop_background([])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_steer_after_last_step_becomes_owned_follow_up(
    tmp_path, monkeypatch, cancel_first
):
    from bus.events import InboundMessage

    runtime = await runtime_at(tmp_path, monkeypatch)
    entered, release, followed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
                return ModelResponse(text="first answer")
            assert messages[-1]["content"].endswith("next request")
            followed.set()
            return ModelResponse(text="follow-up answer")

    model(runtime, Model())
    task = asyncio.create_task(
        runtime.process_direct("first", session_key="test:follow")
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        message = InboundMessage(
            channel="test", sender="human", chat_id="follow", content="next request"
        )
        assert runtime.pipeline.stage(message, "test:follow")
        if cancel_first:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await task
        await asyncio.wait_for(followed.wait(), 5)
        # Wait on the same Session's actual admission to observe durable settlement.
        async with runtime.pipeline.admission.hold("test:follow"):
            statuses = [
                p["status"] for p in runtime.pipeline.programs.store.list("test:follow")
            ]
            assert statuses == (
                ["completed", "paused"] if cancel_first else ["completed", "completed"]
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_legacy_inline_program_facts_migrate_without_changing_ids(
    tmp_path, monkeypatch
):
    from plugins.ledger.owner_store import OwnerTransaction
    from plugins.reply_program.store import OWNER
    from plugins.turn_projection.plugin import TurnProjection

    runtime = await runtime_at(tmp_path, monkeypatch)

    class Model:
        async def acomplete(self, *args, **kwargs):
            return ModelResponse(text="legacy answer")

    model(runtime, Model())
    values = []
    for content in ("legacy first", "legacy second"):
        reply = await runtime.process_direct(content, session_key="test:migration")
        values.append(
            runtime.pipeline.programs.store.read(reply.metadata["program_id"])
        )
    await runtime.stop_background([])
    from plugins.ledger.manager import SessionManager

    sessions = SessionManager(tmp_path)
    with sessions._index_lock:
        sessions._index.execute("BEGIN IMMEDIATE")
        tx = OwnerTransaction(sessions._index)
        for value, input_id in zip(values, ("input_z", "input_a"), strict=True):
            identity = value.pop("id")
            value.pop("revision")
            request = value.pop("request")
            value.pop("result_message_id", None)
            value.pop("journal_version", None)
            value["input_id"] = input_id
            row = tx.read(OWNER, "program:" + identity)
            tx.save(OWNER, "program:" + identity, value, expected_version=row.version)
            tx.save(
                "ledger_inputs",
                input_id,
                {
                    "session_key": "test:migration",
                    "source": "conversation",
                    "request": request,
                    "accepted_at": value["created_at"],
                },
                expected_version=None,
            )
        sessions._index.execute("DELETE FROM ledger_messages")
        sessions._index.commit()
    sessions.close()
    restarted = await runtime_at(tmp_path, monkeypatch)
    try:
        restored = restarted.pipeline.programs.store.read(identity, "test:migration")
        assert restored["result"]["content"] == "legacy answer"
        assert restored["input_id"] == "input_a"
        assert restarted.pipeline.programs.store.store.scan("ledger_inputs") == []
        messages = restarted.session_manager.message_log.read("test:migration")
        assert [
            message.body.parts[1].value
            for message in messages
            if message.message_id in {"input_a", "input_z"}
        ] == ["legacy first", "legacy second"]
        turns = TurnProjection().project(messages, "conversation")
        assert [turn.status for turn in turns] == ["complete", "complete"]
        assert all(len(turn.message_ids) == 2 for turn in turns)
    finally:
        await restarted.stop_background([])


@pytest.mark.asyncio
async def test_socket_steer_withdraw_and_typed_turn_readout(tmp_path, monkeypatch):
    runtime = await runtime_at(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate():
        entered.set()
        await release.wait()
        return "ready"

    runtime.tools.register(ToolSpec("socket_gate", "gate", {"type": "object"}), gate)

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=[ToolCall("socket-gate", "socket_gate", {})]
                )
            assert any(
                m["role"] == "user" and m["content"].endswith("socket correction")
                for m in messages
            )
            assert "withdrawn socket text" not in str(messages)
            return ModelResponse(text="corrected")

    model(runtime, Model())
    await runtime.control_server.start()
    client = await ControlClient.connect(str(runtime.control_server.endpoint))
    try:
        thread = await client.start_thread()
        key = thread["id"]
        handle = await client.start_turn(key, "initial")
        await asyncio.wait_for(entered.wait(), 5)
        assert (
            await client.request(
                "turn/steer",
                {
                    "threadId": key,
                    "messageId": "withdraw",
                    "input": "withdrawn socket text",
                },
            )
        )["staged"]
        assert (
            await client.request(
                "turn/withdraw", {"threadId": key, "messageId": "withdraw"}
            )
        )["withdrawn"]
        assert (
            await client.request(
                "turn/steer",
                {"threadId": key, "messageId": "correct", "input": "socket correction"},
            )
        )["staged"]
        before = await client.request("message/list", {"threadId": key})
        assert [m["body"]["kind"] for m in before["items"]] == ["input", "output"]
        release.set()
        terminal = await asyncio.wait_for(handle.result(), 5)
        assert terminal["status"] == "completed"
        projected = await client.request("turn/project", {"threadId": key})
        assert (
            len(projected["items"]) == 1
            and projected["items"][0]["status"] == "complete"
        )
        after = await client.request("message/list", {"threadId": key, "afterSeq": 1})
        assert sum(m["body"]["kind"] == "input" for m in after["items"]) == 1
        assert not (
            await client.request(
                "turn/withdraw", {"threadId": key, "messageId": "correct"}
            )
        )["withdrawn"]
    finally:
        release.set()
        await client.close()
        await runtime.stop_background([])


def test_turn_projection_excludes_late_abandoned_results_and_other_sources(tmp_path):
    from plugins.ledger.manager import SessionManager
    from plugins.ledger.message import (
        CallRef,
        ContentPart,
        Control,
        Input,
        Output,
        ToolResult,
    )
    from plugins.ledger.message import ToolCall as LedgerCall
    from plugins.turn_projection.plugin import TurnProjection

    sessions = SessionManager(tmp_path)
    ledger = sessions.message_log
    try:
        ledger.append(
            "s", "conversation", "human", Input((ContentPart("text", "old"),))
        )
        call = ledger.append(
            "s",
            "conversation",
            "assistant",
            Output((LedgerCall("read", {}),), "continue"),
        )
        ledger.append("s", "conversation", "runtime", Control("abandon", call.seq))
        ledger.append("s", "job:1", "assistant", Output((), "quiet"))
        late = ledger.append(
            "s",
            "conversation",
            "tool",
            ToolResult(
                CallRef(call.message_id, 0), "success", (ContentPart("text", "late"),)
            ),
        )
        ledger.append(
            "s", "conversation", "human", Input((ContentPart("text", "new"),))
        )
        final = ledger.append(
            "s",
            "conversation",
            "assistant",
            Output((ContentPart("text", "new answer"),), "complete"),
        )
        messages = ledger.read("s")
        projection = TurnProjection()
        turns = projection.project(messages, "conversation")
        assert [turn.status for turn in turns] == ["abandoned", "complete"]
        assert all(
            not turn.observations and late.message_id not in turn.message_ids
            for turn in turns
        )
        assert turns[-1].ending_message_id == final.message_id
        assert projection.project(messages, "job:1")[0].status == "quiet"
        with pytest.raises(ValueError, match="严格递增"):
            projection.project(reversed(messages), "conversation")
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_pending_user_steer_is_not_adopted_by_an_intervening_job(
    tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager
    from bus.events import InboundMessage
    from plugins.ledger.message import Input

    runtime = await runtime_at(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    job_started, job_release, follow_done = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    original_hold = runtime.pipeline.admission.hold
    admissions = 0

    @asynccontextmanager
    async def hold(key):
        nonlocal admissions
        admissions += 1
        first = admissions == 1
        async with original_hold(key):
            yield
        if first:
            # Release the physical lock, then let an already queued job acquire
            # it before the original run settles its volatile steering queue.
            await job_started.wait()

    runtime.pipeline.admission.hold = hold

    class Model:
        calls = 0

        async def acomplete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
                return ModelResponse(text="original answer")
            if self.calls == 2:
                assert not any(
                    "human continuation" in str(message.get("content"))
                    for message in messages
                )
                job_started.set()
                await job_release.wait()
                return ModelResponse(text="job report")
            assert messages[-1]["content"].endswith("human continuation")
            follow_done.set()
            return ModelResponse(text="human answer")

    model(runtime, Model())
    first = asyncio.create_task(
        runtime.process_direct("first", session_key="test:isolated")
    )
    job = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime.pipeline.stage(
            InboundMessage("test", "human", "isolated", "human continuation"),
            "test:isolated",
        )
        job = asyncio.create_task(
            runtime.pipeline.run(
                InboundMessage(
                    "test",
                    "job",
                    "isolated",
                    "job notification",
                    metadata={
                        "source": "job:fixture",
                        "omit_user_turn": True,
                        "skip_post_memory": True,
                    },
                ),
                "test:isolated",
                dispatch_outbound=False,
            )
        )
        # Source acceptance happens before admission, so observing its queued
        # program establishes the intended ordering without timing sleeps.
        async with asyncio.timeout(5):
            while len(runtime.pipeline.programs.store.list("test:isolated")) < 2:
                await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(first, 5)
        job_release.set()
        await asyncio.wait_for(job, 5)
        await asyncio.wait_for(follow_done.wait(), 5)
        async with original_hold("test:isolated"):
            messages = runtime.message_log.read("test:isolated")
            accepted = [
                message
                for message in messages
                if isinstance(message.body, Input)
                and any(
                    part.kind == "text" and part.value == "human continuation"
                    for part in message.body.parts
                )
            ]
            assert len(accepted) == 1 and accepted[0].source == "conversation"
            assert len(runtime.pipeline.programs.store.list("test:isolated")) == 3
    finally:
        release.set()
        job_started.set()
        job_release.set()
        await asyncio.gather(first, *([job] if job else []), return_exceptions=True)
        runtime.pipeline.admission.hold = original_hold
        await runtime.stop_background([])
