"""后台任务完成通知：忙时注入当前回合的下一步，闲时唤醒原会话并回传渠道。"""
import asyncio
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
import pytest_asyncio

from agent.plugin_composition import ServiceKey
from plugins.channels.contract import CHANNEL_INPUT_V2 as CHANNEL_INPUT
from plugins.channels.contract import ChannelInboundMessage
from plugins.jobs.board import JobBoard
from plugins.jobs.contract import JOB_NOTICES, JobOutcome, JobWatch
from plugins.ledger.contract import Input, Output, ToolResult
from tests.test_default_reply import application, live_root

_FIXTURE = '''
import asyncio
from contextlib import asynccontextmanager
from agent.plugin_composition import ServiceKey
from plugins.jobs.contract import JOB_NOTICES, JobOutcome, JobWatch
from plugins.ledger.contract import ContentPart
from plugins.tools.api import Result
from plugins.tools.plugin import TOOLS
api_version = 3
name = "job_fixture"
version = "1.0.0"
inject = (TOOLS, JOB_NOTICES)
async def apply(ctx):
    gate = asyncio.Event()
    class Start:
        idempotent = False
        async def prepare(self, args, source=None):
            return {"session_id": source.messages[-1].session_id, "source": source.messages[-1].source}
        async def invoke(self, key, args):
            async def settled():
                await gate.wait()
                return JobOutcome("completed", "exit_code=0")
            ctx.require(JOB_NOTICES).watch(JobWatch(
                session_id=args["session_id"], source=args["source"], job_id="fake-1", kind="shell",
                label="fixture job", collect_hint="fixture result is ready.", settled=settled,
            ))
            return Result("success", (ContentPart("text", "started"),))
        async def query(self, key):
            return None
    @asynccontextmanager
    async def open(state):
        yield Start()
    await ctx.require(TOOLS).declare_group(ctx, always_on=True)
    await ctx.require(TOOLS).register(ctx, name="start_job", description="start a fixture job",
        parameters={"type": "object"}, open=open)
    await ctx.provide(ServiceKey("fixture.job_gate"), gate)
'''


def _job_sources(sources: Path) -> None:
    from tests.support.delivery_sources import sources as delivery_sources
    delivery_sources(sources)
    shutil.copytree(Path(__file__).parents[1] / "plugins/jobs", sources / "jobs",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (sources / "job_fixture").mkdir()
    (sources / "job_fixture/plugin.py").write_text(_FIXTURE)
    provider = sources / "test_provider/plugin.py"
    provider.write_text(provider.read_text().replace(
        'ToolCall("provider-call", "write_evidence", {})', 'ToolCall("provider-call", "start_job", {})'))


async def _send(host):
    async with live_root(host) as root:
        await root.context.require(CHANNEL_INPUT)("test:room", "u1", ChannelInboundMessage(
            "test", "user", "room", "dispatch the job", datetime(2026, 10, 11, tzinfo=UTC), {},
        ))
        return root.context.require(ServiceKey("fixture.calls")), root.context.require(ServiceKey("fixture.job_gate"))


async def _rows_until(log, done):
    async def wait():
        async for _ in log.catalog().follow():
            rows = log.reader("test:room").snapshot()
            if done(rows):
                return rows
        raise AssertionError("消息订阅提前结束")
    return await asyncio.wait_for(wait(), 5)


def _finished(rows, count):
    return sum(isinstance(row.body, Output) and row.body.finish == "complete" for row in rows) >= count


@pytest.mark.asyncio
async def test_busy_turn_sees_settled_job_on_next_step_without_extra_wake(tmp_path):
    async with application(tmp_path, replying=True, extra_sources=_job_sources) as (log, host):
        calls, gate = await _send(host)
        gate.set()
        rows = await _rows_until(log, lambda rows: _finished(rows, 1))
        # 1. 第二步请求携带结算通知；通知已被本输入领取，回合结束后不再唤醒。
        await asyncio.sleep(0.5)
        assert [type(row.body) for row in log.reader("test:room").snapshot()] == [Input, Output, ToolResult, Output]
        assert len(calls) == 2
        assert "后台任务 fake-1" not in str(calls[0].messages)
        assert "后台任务 fake-1" in str(calls[1].messages)
        assert rows[-1].source == "conversation"


@pytest.mark.asyncio
async def test_idle_session_is_woken_and_reply_is_delivered(tmp_path):
    async with application(tmp_path, replying=True, extra_sources=_job_sources) as (log, host):
        calls, gate = await _send(host)
        await _rows_until(log, lambda rows: _finished(rows, 1))
        assert len(calls) == 2
        # 1. 回合结束后才结算：唤醒一轮，回复归 job 来源并按原渠道发送。
        gate.set()
        rows = await _rows_until(log, lambda rows: _finished(rows, 2))
        assert len(calls) == 3
        assert "后台任务 fake-1" in str(calls[2].messages)
        woken = rows[-1]
        assert woken.source == "job:fake-1"
        await asyncio.wait_for(_delivered(tmp_path, woken.message_id), 5)


async def _delivered(tmp_path: Path, message_id: str) -> None:
    # 发送在唤醒回复提交之后进行；以测试 sender 的发送记录为准。
    while True:
        for sent in (tmp_path / "workspace").rglob("sent.jsonl"):
            if any(json.loads(line)[2] == message_id for line in sent.read_text().splitlines()):
                return
        await asyncio.sleep(0.05)


def _job(job_id: str, *, session_id: str = "s", source: str = "conversation") -> JobWatch:
    async def settled() -> JobOutcome | None:
        return None
    return JobWatch(session_id=session_id, source=source, job_id=job_id, kind="shell",
                    label=job_id, collect_hint="read it.", settled=settled)


def test_board_claim_is_stable_within_one_input_and_retired_by_the_next():
    board = JobBoard()
    notice = board.settle(_job("a"), JobOutcome("completed", "exit_code=0"))
    assert board.claim("s", "conversation", "input-1") == (notice,)
    assert board.claim("s", "conversation", "input-1") == (notice,)
    assert board.claim("other", "conversation", "input-1") == ()
    assert board.claim("s", "subagent", "input-1") == ()
    assert board.claim("s", "conversation", "input-2") == ()
    assert board.pending("s") == ()


def test_board_wake_only_takes_unclaimed_notices_within_budget():
    board = JobBoard(max_consecutive_wakes=1)
    first = board.settle(_job("a"), JobOutcome("completed", "exit_code=0"))
    second = board.settle(_job("b"), JobOutcome("failed", "exit_code=1"))
    # 1. 预算按用户输入连续计数；耗尽后留给下一次输入注入。
    assert board.take_wake(first, "input-1")
    assert not board.take_wake(first, "input-1")
    assert not board.take_wake(second, "input-1")
    assert board.claim("s", "conversation", "input-2") == (second,)
    # 2. 唤醒失败撤回领取，新输入恢复预算。
    board.release_wake(first)
    assert board.take_wake(first, "input-3")
    board.finish_wake(first)
    assert first not in board.pending("s")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "abandon"])
async def test_control_ending_keeps_job_notice_for_next_user_without_rewaking(tmp_path, action):
    from types import SimpleNamespace
    from agent.plugin_composition.tasks import Tasks
    from plugins.jobs.plugin import Jobs, Config, CONVERSATION_COMPLETE, MESSAGE_CATALOG, REPLY_PROGRAM
    from plugins.ledger.contract import Control
    from plugins.ledger.log import MessageLog
    from plugins.sources.session import SourceSession

    log, tasks = MessageLog(tmp_path / "messages.db"), Tasks()
    wake = None
    try:
        def writer(body, *, source="conversation"):
            return log.writer("s", author="fixture", source=source, body_types=(body,), content={})
        conversation = SourceSession(reader=log.reader("s"), inputs=writer(Input),
                                     controls=writer(Control), tasks=tasks)
        await conversation.accept("user", Input(()))
        original = writer(Output).append("original-result", Output((), "complete"))
        calls, delivered = [], []
        entered = asyncio.Event()
        async def program(_task, current, source, _reminders, *, check_admission):
            calls.append(source)
            entered.set()
            if action == "pause":
                await asyncio.Event().wait()
            check_admission()
            return writer(Control, source=source).append(
                "stop-" + action, Control(action, current.head(), "步数耗尽"))
        async def complete(_session, report):
            return await conversation.complete(report)
        services = {MESSAGE_CATALOG: log.catalog(), CONVERSATION_COMPLETE: complete, REPLY_PROGRAM: program}
        jobs = Jobs(SimpleNamespace(require=services.__getitem__), Config())
        async def deliver(message):
            delivered.append(message)
        jobs._deliver = deliver
        notice = jobs._board.settle(_job("finished"), JobOutcome("completed", "exit_code=0"))
        wake = asyncio.create_task(jobs._wake(notice))
        await asyncio.wait_for(entered.wait(), 3)
        if action == "pause":
            stopped = await conversation.pause("user-stop")
            assert stopped.source == "conversation"
        await asyncio.wait_for(wake, 3)
        assert calls == ["job:finished"]
        assert not delivered
        assert notice.claimed_by is None
        await conversation.accept("next-user", Input(()))
        materials = await jobs.materials(log.reader("s").snapshot(), "conversation")
        assert "[status: completed，exit_code=0]" in materials["reminders"][0]["text"]
        assert log.reader("s").get(original.message_id) == original
    finally:
        if wake is not None:
            if not wake.done():
                wake.cancel()
            await asyncio.gather(wake, return_exceptions=True)
        await tasks.close()
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["declined", "exception"])
async def test_declined_job_stop_restores_running_status_without_claiming_user_stopped_it(failure):
    from dataclasses import replace
    from types import SimpleNamespace
    from plugins.jobs.plugin import Jobs, Config

    async def stop():
        if failure == "exception":
            raise RuntimeError("fixture stop rejected")
        return False

    jobs = Jobs(SimpleNamespace(), Config(completion_delivery="quiet"))
    watched = replace(_job("declined"), stop=stop)
    jobs.watch(watched)
    assert not await jobs.stop_job("other", watched.job_id)
    assert jobs._mirror.snapshot(watched.session_id)[0].status == "running"
    assert not await jobs.stop_job(watched.session_id, watched.job_id)
    snapshot = jobs._mirror.snapshot(watched.session_id)[0]
    assert snapshot.status == "running" and snapshot.stoppable
    assert watched.job_id not in jobs._stopped_by_user
    # 生产方稍后移走任务，不能因先前未接纳的停止请求虚构 killed 终态。
    await jobs._follow(watched)
    assert jobs._mirror.snapshot(watched.session_id) == ()
    assert jobs._board.pending(watched.session_id) == ()


@pytest.mark.asyncio
async def test_subagent_capacity_counts_unfinished_work_and_reuses_admitted_effect(tmp_path):
    from plugins.subagent.runtime import SubagentBusy

    def sources(directory):
        fixture = directory / "capacity_fixture"
        fixture.mkdir()
        (fixture / "plugin.py").write_text('''
from agent.plugin_composition import ServiceKey
from agent.plugin_composition.tasks import TASKS
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS, OWNER_STATE, SESSION_ADMISSION
from plugins.subagent.inputs import CONTENT, CHECK_ORIGIN
from plugins.subagent.request import PROFILE_TOOLS, Request
from plugins.subagent.runtime import SUBAGENT_PROGRAM, Subagents
api_version = 3
name = "capacity_fixture"
version = "1.0.0"
inject = (CONTENT, CHECK_ORIGIN, TASKS, BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS, OWNER_STATE, SESSION_ADMISSION)
workspace_roots = ("subagent-runs",)
workspace_files = ("memory/spawn_trace.jsonl",)
async def apply(ctx):
    async def unused_program(*_):
        raise AssertionError("admission must not execute a model")
    await ctx.provide(SUBAGENT_PROGRAM, unused_program)
    binding = ctx.require(BINDINGS).bind(SUBAGENT_PROGRAM, {})
    def request(number):
        return Request(job_id=f"{number:032x}", label="资料研究", profile="research", background=True, retry_count=0,
            parent_session_id="parent", parent_message_id="parent-call", parent_part_index=0,
            origin={"channel": "test", "chat_id": "room", "sender": "user"},
            sink={"name": "test", "binding_id": binding, "address": "room"},
            program_binding=binding, tools={name: binding for name in PROFILE_TOOLS["research"]})
    jobs = Subagents(ctx, max_active=1)
    @ctx.entrypoint
    async def accept(key, request, text):
        await jobs.accept(key, request, text)
    @ctx.entrypoint
    async def read(key):
        return jobs.read(key)
    await ctx.provide(ServiceKey("fixture.subagents"), (accept, read, request(1), request(2), request(3)))
''')

    async with application(tmp_path, replying=False, extra_sources=sources) as (log, host):
        accept, read, first, second, third = host.live_root.context.require(ServiceKey("fixture.subagents"))
        await accept("first", first, "调查资料一")
        await accept("first", first, "调查资料一")
        assert len(log.reader(first.session_id).snapshot()) == 1
        with pytest.raises(SubagentBusy):
            await accept("second", second, "调查资料二")
        assert await read("second") is None and not log.catalog().exists(second.session_id)
        # 原工作已经完成，但通知尚未生成/送达，settled 仍为 False。
        log.writer(first.session_id, author="subagent", source="subagent", body_types=(Output,), content={}).append(
            "first-completed", Output((), "complete"))
        assert (await read("first"))[0].value["settled"] is False
        await accept("second", second, "调查资料二")
        await accept("first", first, "调查资料一")
        assert len(log.reader(first.session_id).snapshot()) == 2
        assert len(log.reader(second.session_id).snapshot()) == 1
        with pytest.raises(SubagentBusy):
            await accept("third", third, "调查资料三")
        assert await read("third") is None and not log.catalog().exists(third.session_id)


@pytest.mark.asyncio
async def test_native_subagent_status_and_original_result_remain_scoped_after_restart(tmp_path):
    from types import SimpleNamespace
    from plugins.delivery.api import Receipt
    from plugins.delivery.execution import Deliveries
    from plugins.delivery.records import Delivery, DeliveryRecords
    from plugins.ledger.contract import CallRef, ContentPart, ContentReferences, Control, SessionAttributes
    from plugins.ledger.log import MessageLog
    from plugins.subagent.inputs import DELIVERY
    from plugins.subagent.request import Request, PROFILE_TOOLS, check_request
    from plugins.subagent.runtime import Subagents, MESSAGE_CATALOG, OWNER_STATE
    from plugins.subagent.tools import Manage
    from plugins.tools.contract import CallSource

    path = tmp_path / "messages.db"
    log = MessageLog(path)
    def service():
        state, catalog = log.owner("subagent"), log.catalog()
        records = DeliveryRecords(log.owner("delivery"), "subagent")
        delivery = Deliveries(records, catalog, None, None, task_key="read-only-test")
        services = {OWNER_STATE: SimpleNamespace(open=lambda _ctx: state), MESSAGE_CATALOG: catalog,
                    DELIVERY: SimpleNamespace(open=lambda _ctx: delivery)}
        return Subagents(SimpleNamespace(require=services.__getitem__)), state, records, delivery

    def seed(number, parent, *, text=None, control=None, reason="原始取消原因", settled=False):
        job_id = f"{number:032x}"
        request = Request(job_id=job_id, label="资料研究", profile="research", background=True,
            retry_count=0, parent_session_id=parent, parent_message_id="parent-call", parent_part_index=0,
            origin={"channel": "test", "chat_id": parent, "sender": "user"},
            sink={"name": "test", "binding_id": "sender", "address": parent},
            program_binding="program", tools={name: name for name in PROFILE_TOOLS["research"]})
        log.ensure_session(request.session_id, SessionAttributes(visibility="internal", learning="excluded"))
        log.writer(request.session_id, author="subagent", source="subagent", body_types=(Input,),
            content={"subagent.request": lambda part: check_request(part, check_origin=lambda _part: ContentReferences())}).append(
                request.input_id, Input((ContentPart("subagent.request", request.model_dump(mode="json")),)))
        state.transact(lambda tx: tx.save("job-" + job_id,
            {"session_id": request.session_id, "input_id": request.input_id, "settled": settled}, expected_version=None))
        if text is not None:
            log.writer(request.session_id, author="subagent", source="subagent", body_types=(Output,),
                content={"text": lambda _part: ContentReferences()}).append("answer-" + job_id,
                    Output((ContentPart("text", text),), "complete"))
        if control:
            log.writer(request.session_id, author="subagent", source="subagent", body_types=(Control,), content={}).append(
                "control-" + job_id, Control(control, 0, reason))
        return request

    try:
        for binding in ("program", "sender", *PROFILE_TOOLS["research"]):
            log.save_binding(binding, {"fixture": binding})
        jobs, state, records, delivery = service()
        pending = seed(1, "parent")
        seed(2, "other", text="SECRET_OTHER", settled=True)
        original = "原始结果🙂" * 1000
        done = seed(3, "parent", text=original, settled=True)
        cancelled = seed(4, "parent", control="pause", settled=True)
        parent_notice = log.writer("parent", author="agent", source=done.session_id, body_types=(Output,),
            content={"text": lambda _part: ContentReferences()}).append("notice", Output((ContentPart("text", "简短通知"),), "complete"))
        await delivery.prepare_async(log.reader("parent"), parent_notice, (done.sink,))
        record, prepared = records.read("notice", "test")
        await records.save_async("notice", record, Delivery(sink=prepared.sink, phase="failed",
            receipt=Receipt(status="failed", error="渠道暂不可用")))
        log.writer("parent", author="agent", source="conversation", body_types=(Output,), content={}).append("manage-call", Output((), "continue"))
        caller = log.reader("parent").get("manage-call")
        manage = Manage(jobs)
        source = CallSource(CallRef("manage-call", 0), (caller,))
        async def invoke(arguments, *, caller_source=source):
            prepared = await manage.prepare(arguments, caller_source)
            assert isinstance(prepared, dict), prepared
            response = await manage.invoke("query", prepared)
            return response.outcome, str(response.parts[0].value)
        outcome, payload = await invoke({"action": "list"})
        assert outcome == "success" and [item["job_id"] for item in json.loads(payload)["jobs"]] == [pending.job_id]
        outcome, payload = await invoke({"action": "list", "include_settled": True, "limit": 1})
        assert outcome == "success" and len(json.loads(payload)["jobs"]) == 1 and json.loads(payload)["has_more"]
        outcome, payload = await invoke({"action": "status", "job_id": done.job_id})
        status = json.loads(payload)
        assert status["work_status"] == "completed" and status["notification"]["status"] == "failed"
        assert status["notification"]["message"]["message_id"] == parent_notice.message_id
        outcome, payload = await invoke({"action": "result", "job_id": done.job_id, "offset": 5, "length": 17})
        page = json.loads(payload)["result"]
        assert page["text"] == original[5:22] and page["next_offset"] == 22 and page["source_chars"] == len(original)
        assert page["source"]["message_id"] == "answer-" + done.job_id and page["source"]["recorded_at"]
        assert json.loads((await invoke({"action": "status", "job_id": cancelled.job_id}))[1])["work_status"] == "cancelled"
        for action in ("status", "result", "cancel", "stop"):
            outcome, payload = await invoke({"action": action, "job_id": f"{2:032x}"})
            assert outcome == "error" and "不属于当前会话" in payload and "SECRET" not in payload
        assert isinstance(await manage.prepare({"action": "list", "parent_session_id": "other"}, source), str)
        assert isinstance(await manage.prepare({"action": "list"}), str)
        exhausted = seed(5, "parent", control="abandon", reason="执行步数耗尽")
        exhausted_reader = log.reader(exhausted.session_id)
        assert await jobs.outcome(exhausted_reader, through_seq=0) is None
        assert await jobs.outcome(exhausted_reader) == ("failed", "执行步数耗尽")
        phases = []
        async def trace(request, phase):
            phases.append((request.job_id, phase))
        jobs._trace = trace
        # 恢复时只读取已保存的 abandon；上下文没有程序绑定，任何重复模型执行都会失败。
        await jobs._run(SimpleNamespace(active=True), "job-" + exhausted.job_id)
        assert phases == [(exhausted.job_id, "failed")]
        exhausted_status = await jobs.inspect("parent", job_id=exhausted.job_id, result=True)
        assert exhausted_status["work_status"] == "failed"
        assert exhausted_status["result"]["text"] == "执行步数耗尽"
        before = log.reader(done.session_id).snapshot()
        log.close()
        log = MessageLog(path)
        jobs, state, records, delivery = service()
        restored = await jobs.inspect("parent", job_id=done.job_id, result=True, offset=22, length=6000)
        assert restored["result"]["text"] == original[22:6022]
        assert restored["notification"]["status"] == "failed"
        assert log.reader(done.session_id).snapshot() == before
        assert await jobs.outcome(log.reader(exhausted.session_id)) == ("failed", "执行步数耗尽")
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["pause", "abandon", "exception"])
async def test_subagent_callback_waits_for_real_user_after_stop_or_failure_across_restart(tmp_path, ending):
    from contextlib import aclosing, asynccontextmanager
    from types import SimpleNamespace
    from agent.plugin_composition.tasks import TASKS, Tasks
    from plugins.delivery.api import Receipt
    from plugins.delivery.execution import Deliveries
    from plugins.delivery.records import Delivery, DeliveryRecords
    from plugins.ledger.contract import ContentPart, ContentReferences, Control
    from plugins.ledger.log import MessageLog
    from plugins.sources.session import SourceSession
    from plugins.subagent.inputs import CONVERSATION_COMPLETE, DELIVERY, REPLY_PROGRAM
    from plugins.subagent.request import PROFILE_TOOLS, Request, check_request
    from plugins.subagent.runtime import MESSAGE_CATALOG, MESSAGE_WRITERS, OWNER_STATE, Subagents, _notice_state

    path, calls = tmp_path / "messages.db", []
    request = Request(job_id="f" * 32, label="后台资料", profile="research", background=True, retry_count=0,
        parent_session_id="parent", parent_message_id="parent-answer", parent_part_index=0,
        origin={"channel": "test", "chat_id": "room", "sender": "user"},
        sink={"name": "test", "binding_id": "sender", "address": "room"},
        program_binding="program", tools={name: name for name in PROFILE_TOOLS["research"]})
    mode = ending
    entered = asyncio.Event()

    @asynccontextmanager
    async def running():
        log, tasks = MessageLog(path), Tasks()
        def writer(session, body, *, source="conversation"):
            return log.writer(session, author="fixture", source=source, body_types=(body,),
                              content={"text": lambda _part: ContentReferences()})
        conversation = SourceSession(reader=log.reader("parent"), inputs=writer("parent", Input),
                                     controls=writer("parent", Control), tasks=tasks)
        state = log.owner("subagent")
        records = DeliveryRecords(log.owner("delivery"), "subagent")
        class RecordedDelivery(Deliveries):
            async def send(self, message_id, sink):
                message = log.reader("parent").get(message_id)
                assert isinstance(message.body, Output) and message.body.finish == "complete"
                row, prepared = records.read(message_id, sink)
                receipt = Receipt(status="delivered")
                await records.save_async(message_id, row, Delivery(sink=prepared.sink, phase="delivered", receipt=receipt))
                return receipt
        delivery = RecordedDelivery(records, log.catalog(), tasks, None, task_key="fixture")
        async def complete(_session, report):
            return await conversation.complete(report)
        async def program(task, current, source, _extra, *, check_admission):
            calls.append(mode)
            entered.set()
            if mode == "pause":
                await asyncio.Event().wait()
            if mode == "exception":
                raise RuntimeError("PRIVATE PROVIDER BODY MUST NEVER BE STORED")
            body_type = Control if mode == "abandon" else Output
            output = writer("parent", body_type, source=source)
            task.on_close(output.expire)
            check_admission()
            body = (Control("abandon", current.head(), "回访步数耗尽") if mode == "abandon"
                    else Output((ContentPart("text", "调查完成，原始结果仍然可读。"),), "complete"))
            return await output.append_async("callback-" + mode, body)
        def bind(_ctx, **kwargs):
            return lambda session: log.writer(session, **kwargs)
        @asynccontextmanager
        async def scope():
            yield
        services = {MESSAGE_CATALOG: log.catalog(), MESSAGE_WRITERS: SimpleNamespace(bind=bind),
                    OWNER_STATE: SimpleNamespace(open=lambda _ctx: state), DELIVERY: SimpleNamespace(open=lambda _ctx: delivery),
                    TASKS: SimpleNamespace(open=lambda _ctx: tasks), CONVERSATION_COMPLETE: complete, REPLY_PROGRAM: program}
        ctx = SimpleNamespace(require=services.__getitem__, runtime_scope=scope,
                              workspace_file=lambda relative: tmp_path / relative,
                              workspace_root=lambda relative: tmp_path / relative)
        jobs = Subagents(ctx)
        try:
            yield log, state, conversation, jobs, lambda: asyncio.create_task(jobs.follow()), delivery
        finally:
            # Tests own the native follower and all Tasks; no daemon is left alive between restarts.
            await tasks.close()
            log.close()

    async def status_until(jobs, predicate):
        async with asyncio.timeout(5):
            while True:
                status = await jobs.inspect("parent", job_id=request.job_id)
                if predicate(status):
                    return status
                await asyncio.sleep(0.01)

    async with running() as (log, state, conversation, jobs, start, delivery):
        for binding in ("program", "sender", *PROFILE_TOOLS["research"]):
            log.save_binding(binding, {"fixture": binding})
        await conversation.accept("user-original", Input(()))
        log.writer("parent", author="agent", source="conversation", body_types=(Output,), content={}).append(
            "parent-answer", Output((), "complete"))
        log.writer(request.session_id, author="subagent", source="subagent", body_types=(Input,),
            content={"subagent.request": lambda part: check_request(part, check_origin=lambda _part: ContentReferences())}).append(
                request.input_id, Input((ContentPart("subagent.request", request.model_dump(mode="json")),)))
        state.transact(lambda tx: tx.save("effect", {"session_id": request.session_id,
            "input_id": request.input_id, "settled": False}, expected_version=None))
        log.writer(request.session_id, author="subagent", source="subagent", body_types=(Output,),
            content={"text": lambda _part: ContentReferences()}).append("original-result", Output((ContentPart("text", "完整原始结果"),), "complete"))
        original = log.reader(request.session_id).snapshot()
        watcher = start()
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if ending == "pause":
                await conversation.pause("user-stop")
            blocked = await status_until(jobs, lambda item: item["notification"]["stage"] == "waiting_for_user")
            assert blocked["work_status"] == "completed" and not blocked["settled"]
            assert blocked["notification"]["status"] == ("paused" if ending == "pause" else "blocked")
            controls = [row for row in log.reader("parent").snapshot()
                        if row.source == request.session_id and isinstance(row.body, Control)]
            assert [row.body.action for row in controls] == ([] if ending == "pause" else ["abandon" if ending == "abandon" else "failure"])
            log.writer("parent", author="agent", source="unrelated", body_types=(Output,), content={}).append(
                "unrelated-output", Output((), "complete"))
            log.writer("other", author="user", source="conversation", body_types=(Input,), content={}).append(
                "other-user", Input(()))
            await asyncio.sleep(0.1)
            assert calls == [ending] and delivery.pending() == ()
            assert "PRIVATE PROVIDER BODY" not in str(log.reader("parent").snapshot())
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
    async with running() as (log, _state, conversation, jobs, start, delivery):
        # 重启后将首次回访停在已读到旧控制、尚未退出 active 的边界。
        # Input 及其正常回复的目录事件都先被消费，再放行退出；不能靠第三条消息唤醒。
        observed, release, consumed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        catalog = jobs.ctx.require(MESSAGE_CATALOG)
        target = None
        class ObservedCatalog:
            def __getattr__(self, name):
                return getattr(catalog, name)
            async def follow(self):
                async with aclosing(catalog.follow()) as changes:
                    async for heads in changes:
                        yield heads
                        if target is not None and heads.get("parent", -1) >= target:
                            consumed.set()
        require = jobs.ctx.require
        wrapped_catalog = ObservedCatalog()
        jobs.ctx.require = lambda key: wrapped_catalog if key == MESSAGE_CATALOG else require(key)
        announce = jobs._announce
        async def gated_announce(*args):
            if not observed.is_set():
                _, blocked = await catalog.reader("parent").read_async(
                    lambda reader: _notice_state(reader, request.session_id))
                assert blocked is not None
                observed.set()
                await release.wait()
                return False
            return await announce(*args)
        jobs._announce = gated_announce
        watcher = start()
        try:
            await asyncio.wait_for(observed.wait(), 5)
            await status_until(jobs, lambda item: item["notification"]["stage"] == "waiting_for_user")
            await asyncio.sleep(0.1)
            assert calls == [ending] and delivery.pending() == ()
            mode = "success"
            await conversation.accept("user-followup", Input(()))
            target = log.reader("parent").head() + 1
            async def reply(task, reader, source):
                output = log.writer("parent", author="agent", source=source, body_types=(Output,), content={})
                task.on_close(output.expire)
                return await output.append_async("followup-answer", Output((), "complete"))
            task = await conversation.start(reply)
            assert task is not None
            answer = await task.join()
            assert answer.seq == target
            await asyncio.wait_for(consumed.wait(), 5)
            release.set()
            completed = await status_until(jobs, lambda item: item["settled"])
            assert calls == [ending, "success"]
            assert completed["notification"]["status"] == "delivered"
            assert log.reader(request.session_id).snapshot() == original
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher


def _shell_sources(sources: Path) -> None:
    _job_sources(sources)
    provider = sources / "test_provider/plugin.py"
    provider.write_text(provider.read_text().replace(
        'ToolCall("provider-call", "start_job", {})',
        'ToolCall("provider-call", "shell", {"command": "sleep 1; echo background-done", "shell": "bash", "login": False, '
        '"description": "fixture background", "run_in_background": True, "yield_time_ms": 250})',
    ))


def _collecting_shell_sources(sources: Path, *, tool: str, chain: bool) -> None:
    """夹具模型收到通知后实际领取或停止后台进程，不只检查提醒文字。"""
    _shell_sources(sources)
    provider = sources / "test_provider/plugin.py"
    text = provider.read_text().replace('return LLMResponse("finished")', '''if len(calls) in (3, 6):
                import re
                execution_id = int(re.findall(r"后台任务 shell-(\\d+)", str(request.messages))[-1])
                return LLMResponse(None, [ToolCall("collect-call", COLLECT_TOOL, {"execution_id": execution_id})])
            return LLMResponse("finished")'''.replace("COLLECT_TOOL", repr(tool)))
    if chain:
        text = text.replace('return LLMResponse("finished")', '''if len(calls) == 4:
                return LLMResponse(None, [ToolCall("next-job", "shell", {
                    "command": "sleep 1; echo next-background-done", "description": "next background",
                    "shell": "bash", "login": False,
                    "run_in_background": True, "yield_time_ms": 250})])
            return LLMResponse("finished")''')
    provider.write_text(text)


# A cold import of this project in a new interpreter, on storage that may be a
# network filesystem. Generous on purpose: the fixture is not measuring startup.
BRIDGE_SOCKET_TIMEOUT_S = 180.0
BRIDGE_SHUTDOWN_TIMEOUT_S = 30.0


@pytest_asyncio.fixture(params=["local", "host-bridge"])
async def shell_backend(tmp_path, monkeypatch, request):
    """真实 Socket 和服务端复跑后台 Shell，核对本地与远端的可观察语义。"""
    if request.param == "local":
        yield
        return
    from plugins.host_execution.bridge.boot import claim_host_bridge_boot
    socket = tmp_path.parent / "bridge.sock"
    for name, value in {
        "AKASHIC_EXECUTION_MODE": "host-bridge", "AKASHIC_HOST_BRIDGE_SOCKET": str(socket),
        "AKASHIC_HOST_BRIDGE_TOKEN": "jobs-fixture", "AKASHIC_BOOT_ID": "jobs-fixture",
        "AKASHIC_RUNTIME_COMMIT": "a" * 40, "AKASHIC_HOST_TOOLCHAIN_DIGEST": "b" * 64,
    }.items():
        monkeypatch.setenv(name, value)
    token_file = tmp_path / "bridge.token"
    token_file.write_text("jobs-fixture")
    # Bridge 独立进程也能响应同步的环境探测，和正式部署保持相同边界。
    with (tmp_path / "bridge.log").open("w") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "plugins.host_execution.bridge.server",
            "--socket", str(socket), "--token-file", str(token_file), "--lease-timeout", "60",
            "--artifact-root", str(tmp_path / "artifacts"), "--release-commit", "a" * 40,
            "--toolchain-digest", "b" * 64, "--runtime-checkout", str(Path(__file__).parents[1]),
            "--bridge-python", sys.executable, stdout=log, stderr=log,
        )
        try:
            # Publishing the socket costs one cold import of this project in a
            # fresh interpreter. On a checkout served over NFS that read is
            # dominated by the filesystem, not by the bridge: measured here at
            # 106s, 31s and 2.3s on successive runs as the page cache warmed,
            # against roughly 23k module files in the environment. A few seconds
            # is therefore a statement about the filesystem rather than about
            # the code under test, so the budget is generous and the failure
            # says what it was waiting for.
            start = time.monotonic()
            while not socket.exists():
                waited = time.monotonic() - start
                if process.returncode is not None:
                    raise AssertionError(
                        f"host bridge exited with {process.returncode} after {waited:.1f}s\n"
                        + (tmp_path / "bridge.log").read_text())
                if waited > BRIDGE_SOCKET_TIMEOUT_S:
                    process.terminate()
                    raise AssertionError(
                        f"host bridge published no socket in {waited:.1f}s while still running; "
                        f"a cold import on slow storage can exceed this\n"
                        + (tmp_path / "bridge.log").read_text())
                await asyncio.sleep(0.01)
            assert await claim_host_bridge_boot() is not None
            yield
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), BRIDGE_SHUTDOWN_TIMEOUT_S)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,chain", [("write_stdin", False), ("task_stop", False), ("write_stdin", True)])
async def test_background_shell_outlives_turn_and_wakes_session(tmp_path, tool, chain, shell_backend):
    async with application(tmp_path, replying=True, extra_sources=partial(_collecting_shell_sources, tool=tool, chain=chain),
                           shell=True) as (log, host):
        calls, _gate = await _send(host)
        rows = await _rows_until(log, lambda rows: _finished(rows, 1))
        # 1. 首个窗口内仍在运行：工具结果给出后台句柄，回合正常结束。
        result = next(row for row in rows if isinstance(row.body, ToolResult))
        text = "\n".join(str(part.value) for part in result.body.parts)
        assert '"process_status": "running"' in text and "已转入后台" in text
        # 2. 回合清理不回收后台分区；进程自然退出后唤醒原会话。
        rows = await _rows_until(log, lambda rows: _finished(rows, 2))
        assert rows[-1].source.startswith("job:shell-")
        assert "status: completed，exit_code=0" in str(calls[2].messages)
        collected = [row for row in rows if isinstance(row.body, ToolResult)][1]
        assert collected.body.outcome == "success"
        text = "\n".join(str(part.value) for part in collected.body.parts)
        assert ('background-done' if tool == "write_stdin" else '"status": "stopped"') in text
        if chain:
            # 唤醒中追加的后台任务仍归原来源，下一次结算也能唤醒并读取。
            rows = await _rows_until(log, lambda rows: _finished(rows, 3))
            collected = [row for row in rows if isinstance(row.body, ToolResult)][-1]
            assert collected.body.outcome == "success"
            assert "next-background-done" in str(collected.body.parts)


def _gated_wake_sources(sources: Path) -> None:
    """将唤醒的模型请求停在明确边界，观察新输入和卸载的真实取消路径。"""
    _job_sources(sources)
    provider = sources / "test_provider/plugin.py"
    text = provider.read_text().replace("calls = []", """calls = []
    import asyncio
    wake_started, wake_release, wake_exited = asyncio.Event(), asyncio.Event(), asyncio.Event()""")
    text = text.replace('return LLMResponse("finished")', '''if len(calls) == 3:
                wake_started.set()
                try:
                    await wake_release.wait()
                finally:
                    wake_exited.set()
            return LLMResponse("finished")''')
    text += '\n    await ctx.provide(ServiceKey("fixture.wake_events"), (wake_started, wake_release, wake_exited))\n'
    provider.write_text(text)


@pytest.mark.asyncio
async def test_new_input_claims_notice_from_preempted_wake(tmp_path):
    async with application(tmp_path, replying=True, extra_sources=_gated_wake_sources) as (log, host):
        calls, gate = await _send(host)
        original = await _rows_until(log, lambda rows: _finished(rows, 1))
        async with live_root(host) as root:
            started, release, exited = root.context.require(ServiceKey("fixture.wake_events"))
            gate.set()
            try:
                await asyncio.wait_for(started.wait(), 5)
                await root.context.require(CHANNEL_INPUT)("test:room", "u2", ChannelInboundMessage(
                    "test", "user", "room", "new user message", datetime(2026, 10, 11, tzinfo=UTC), {},
                ))
                rows = await _rows_until(log, lambda rows: _finished(rows, 2))
                assert exited.is_set()
                assert "后台任务 fake-1" in str(calls[3].messages)
                assert rows[-1].source == "conversation"
                assert rows[:len(original)] == original
            finally:
                release.set()


@pytest.mark.asyncio
async def test_jobs_unload_cancels_active_wake(tmp_path):
    async with application(tmp_path, replying=True, extra_sources=_gated_wake_sources) as (log, host):
        _calls, gate = await _send(host)
        original = await _rows_until(log, lambda rows: _finished(rows, 1))
        async with live_root(host) as root:
            started, release, exited = root.context.require(ServiceKey("fixture.wake_events"))
            jobs = root.context.require(JOB_NOTICES)
            handle = jobs._ctx.fiber
            gate.set()
            try:
                await asyncio.wait_for(started.wait(), 5)
                # 插件卸载必须主动取消请求，不能等夹具放行后才完成。
                async with asyncio.timeout(5):
                    await handle.dispose()
                assert exited.is_set()
                assert log.reader("test:room").snapshot() == original
            finally:
                release.set()
