import asyncio
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from typing import cast

import pytest

from agent.plugin_composition.tasks import Tasks
from plugins.ledger.contract import (
    ContentPart,
    ContentReferences,
    Control,
    Input,
    Output,
)
from plugins.ledger.log import MessageLog, WriterExpired
from plugins.sources.session import SourceSession as Conversation


@pytest.mark.asyncio
async def test_abandon_command_is_explicit_idempotent_and_keeps_history(tmp_path):
    from datetime import UTC, datetime

    from plugins.channels.contract import CHANNEL_INPUT_V2, ChannelInboundMessage
    from plugins.ledger.contract import MessageConflict
    from tests.test_default_reply import application

    async with application(tmp_path, replying=False) as (log, host):
        accept = host.live_root.context.require(CHANNEL_INPUT_V2)

        async def send(identity, text):
            return await accept("test:abandon", identity, ChannelInboundMessage(
                "test", "user", "abandon", text, datetime.now(UTC), {}))

        original = await send("original", "keep the original task and its history")
        ended = await send("stop-old-work", "/abandon@fixture_bot")
        assert isinstance(ended.body, Control) and ended.body.action == "abandon"
        assert ended.body.through_seq == original.seq
        assert await send("stop-old-work", "/abandon@fixture_bot") == ended
        with pytest.raises(MessageConflict, match="已经关闭"):
            await send("stop-again", "/abandon")
        assert log.reader("test:abandon").snapshot() == (original, ended)
        fresh = await send("fresh", "a new question")
        assert isinstance(fresh.body, Input)
        assert log.reader("test:abandon").snapshot() == (original, ended, fresh)


@pytest.mark.asyncio
async def test_installed_reply_exhaustion_closes_its_source_and_answers_next_input(tmp_path):
    from types import SimpleNamespace

    from agent.plugin_composition.config_input import save_config
    from kirakira_agent.runtime import submit
    from plugins.react.plugin import STEP_LIMIT_ABANDON
    from tests.test_default_reply import application

    def configure(sources):
        save_config(sources.parent / "workspace/plugin-data/reply-builtin", {
            "max_steps": 1, "max_output_tokens": 4096,
        })

    async with application(tmp_path, replying=True, extra_sources=configure) as (log, host):
        core = SimpleNamespace(plugin_manager=host)
        with pytest.raises(RuntimeError, match="native reply abandon"):
            await submit(core, "akashic:exhaustion", "original", "finish this task", timeout=5)
        stopped = log.reader("akashic:exhaustion").snapshot()[-1]
        assert stopped.body.reason.startswith(STEP_LIMIT_ABANDON)
        prefix = log.reader("akashic:exhaustion").snapshot()
        answered = await submit(core, "akashic:exhaustion", "fresh", "a fresh question", timeout=5)
        assert isinstance(answered.body, Output) and answered.body.finish == "complete"
        assert answered.seq > stopped.seq
        assert (tmp_path / "effect.txt").read_text() == "once\n"
        rows = log.reader("akashic:exhaustion").snapshot()
        assert rows[:len(prefix)] == prefix
        assert not any(isinstance(row.body, Control) and row.body.action == "failure" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("command,action", [("/stop", "pause"), ("/abandon", "abandon")])
async def test_submit_control_receipt_ends_wait_without_an_assistant_output(tmp_path, command, action):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from kirakira_agent.runtime import submit
    from plugins.channels.contract import CHANNEL_INPUT_V2, ChannelInboundMessage
    from tests.test_default_reply import application

    async with application(tmp_path, replying=False) as (log, host):
        first = await host.live_root.context.require(CHANNEL_INPUT_V2)(
            "akashic:control", "first", ChannelInboundMessage(
                "akashic", "user", "control", "original task", datetime.now(UTC), {}))
        with pytest.raises(RuntimeError, match="native reply " + action):
            await submit(SimpleNamespace(plugin_manager=host), "akashic:control", "control", command, timeout=1)
        rows = log.reader("akashic:control").snapshot()
        assert rows[0] == first and len(rows) == 2
        assert isinstance(rows[-1].body, Control) and rows[-1].body.action == action


@pytest.mark.asyncio
async def test_session_links_admit_coordinates_and_read_live_bounded_pages(tmp_path):
    import json
    import shutil
    from pathlib import Path

    from plugins.channels.contract import CHANNEL_INPUT_V2, InputPartRejected
    from plugins.gateway.protocol.models import MessageSendParams
    from plugins.gateway.service import ControlService
    from plugins.ledger.contract import CallRef, json_value
    from plugins.session_reference.plugin import MAX_PAGE_CHARS, ReadSession
    from plugins.tools.contract import CallSource
    from tests.test_default_reply import application

    def sources(directory):
        shutil.copytree(Path(__file__).parents[1] / 'plugins/session_reference', directory / 'session_reference')

    async with application(tmp_path, replying=False, extra_sources=sources) as (log, host):
        writer = log.writer('linked', author='user', source='conversation', body_types=(Input,),
                            content={'text': lambda _: ContentReferences()})
        for number in range(60):
            writer.append('linked-'+str(number), Input((ContentPart('text', str(number)+':'+'evidence '*500),)))
        accepted = host.live_root.context.require(CHANNEL_INPUT_V2)
        control = ControlService(log.catalog(), tmp_path, accept=accepted, reply_status=lambda _:None,
                                 attachments=lambda _:(), control_frames=None)
        value = {'sessions': [{'session_id': 'linked', 'title': 'Reference title'}]}
        async def send(identity, references):
            receipt = await control.send_message(MessageSendParams(session_id='akashic:current', message_id=identity,
                parts=[{'kind':'session.reference','value':references}]))
            return log.reader('akashic:current').get(receipt['message_id'])
        original = await send('link-input', value)
        assert len(json.dumps(json_value(original.body.parts[-1].value))) < 200
        source = CallSource(CallRef('call', 0), log.reader('akashic:current').snapshot())
        tool = ReadSession(log.catalog())
        assert isinstance(await tool.prepare({'session_id':'elsewhere'}, source), str)
        assert isinstance(await tool.prepare({'session_id':'linked'}, source), dict)
        found=[];before=None
        while True:
            args={'session_id':'linked','limit':50}
            if before is not None:args['before_seq']=before
            result=await tool.invoke('read',args)
            page=json.loads(result.parts[0].value)
            assert sum(len(row['text']) for row in page['messages']) <= MAX_PAGE_CHARS
            found.extend(row['seq'] for row in page['messages'])
            if not page['has_more']:break
            assert page['next_before_seq'] is not None and page['next_before_seq'] != before
            before=page['next_before_seq']
        assert sorted(found)==list(range(60)) and len(set(found))==60
        writer.append('new-decision', Input((ContentPart('text','Latest decision: budget 900.'),)))
        updated=json.loads((await tool.invoke('live',{'session_id':'linked'})).parts[0].value)
        assert updated['messages'][-1]['text']=='Latest decision: budget 900.'
        expanded=json.loads((await tool.invoke('original',{
            'session_id':'linked','message_id':'linked-59','offset':4000})).parts[0].value)
        assert expanded['text']==('59:'+'evidence '*500)[4000:]
        assert expanded['next_offset'] is None and expanded['source_chars']>4000
        assert (await tool.invoke('foreign',{'session_id':'linked','message_id':'link-input'})).outcome=='error'
        assert isinstance(await tool.prepare({'session_id':'linked','offset':1},source),str)
        assert log.reader('akashic:current').snapshot()==(original,)
        for refs in [{'sessions':[{'session_id':'akashic:current'}]}, {'sessions':[{'session_id':'missing'}]},
                     {'sessions':[{'session_id':'linked'},{'session_id':'linked'}]}]:
            with pytest.raises(InputPartRejected):await send('bad-reference',refs)
        assert log.reader('akashic:current').snapshot()==(original,)

@asynccontextmanager
async def source(tmp_path, run, *, changed=None):
    log = MessageLog(tmp_path / "sessions.db")
    tasks = Tasks()
    def writer(body, *, source="conversation", author="app", call_ref=None):
        return log.writer("s", author=author, source=source, body_types=(body,),
                          content={"text": lambda part: ContentReferences()}, call_ref=call_ref,
                          check_call=lambda call: None)
    async def program(task, reader, source):
        output = writer(Output, source=source)
        task.on_close(output.expire)
        return await run(task, reader, output)
    conversation = Conversation(
        reader=log.reader("s"), inputs=writer(Input), controls=writer(Control),
        tasks=tasks, changed=changed,
    )
    try:
        yield conversation, log, writer, program
    finally:
        await tasks.close()
        log.close()

@pytest.mark.asyncio
async def test_interrupt_inputs_survive_and_old_output_cannot_commit(tmp_path):
    entered = asyncio.Event()
    drain = asyncio.Event()
    cancelled = asyncio.Event()
    writers = []
    async def run(task, reader, writer):
        writers.append(writer)
        if len(writers) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await drain.wait()
        _ = reader.snapshot()
        return writer.append("answer", Output((ContentPart("text", "answer"),), "complete"),
                             expected_source_head=reader.head(source="conversation"))
    async with source(tmp_path, run) as (conversation, log, writer, program):
        first = await conversation.accept("u1", Input(()))
        _ = await conversation.start(program)
        await entered.wait()
        await conversation.accept("u2", Input(()))
        await cancelled.wait()
        replacement = asyncio.create_task(conversation.start(program))
        await conversation.accept("u3", Input(()))
        writer(Output, source="wake").append("proactive", Output((), "complete"))
        with pytest.raises(WriterExpired):
            writers[0].append("stale", Output((), "complete"))
        assert len(writers) == 1
        drain.set()
        latest = await replacement
        await latest.join()
        assert [m.message_id for m in log.reader("s").read()] == ["u1", "u2", "u3", "proactive", "answer"]
        assert await conversation.start(program) is None
        assert await conversation.accept("u1", Input(())) == first
        assert await conversation.start(program) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", [False, True])
async def test_source_commit_drains_before_cancel_and_rejects_late_start(tmp_path, monkeypatch, closing):
    """C3/C4/C5：取消不丢提交收据，新输入先提交时旧 intent 不能启动效果。"""
    import sqlite3
    from contextlib import closing as close_connection

    from agent.plugin_composition.tasks import TaskServiceClosed
    from plugins.sources.session import check_source
    from tests.test_akasha_execution import WorkerGate, loop_turn

    changed = []
    started = asyncio.Event()
    outputs = []

    def record(reader, name, pending):
        asyncio.get_running_loop()
        changed.append((name, reader.head()))

    async def run(task, reader, writer):
        outputs.append(writer)
        started.set()
        await asyncio.Event().wait()

    async with source(tmp_path, run, changed=record) as (conversation, log, make_writer, program):
        original = await conversation.accept("first", Input(()))
        task = await conversation.start(program)
        assert task is not None
        await started.wait()
        gate = WorkerGate()
        original_write = log._write
        stopped = False

        def committed(callback):
            nonlocal stopped
            result = original_write(callback)
            if not stopped and log.reader("s").get("second") is not None:
                stopped = True
                gate.stop()
            return result

        monkeypatch.setattr(log, "_write", committed)
        job = asyncio.create_task(conversation.accept("second", Input(())))
        state = log.owner("first-effect")
        close_job = None
        queued = None
        effect = None
        try:
            # 1. SQL 已提交，loop 通知还没运行；旧 Task 此时仍有 active 权限。
            await gate.wait(job)
            assert task.active
            with log.reader("s").read_snapshot():
                assert log.reader("s").get("second") is not None
            await loop_turn()
            job.cancel()

            def claim(tx):
                check_source(task, log.reader("s"), "conversation", original.seq, transaction=tx)
                return tx.save("started", {"phase": "started"}, expected_version=None)

            effect = asyncio.create_task(state.transact_async(claim))
            if closing:
                queued = asyncio.create_task(conversation.start(program))
                await loop_turn()
                close_job = asyncio.create_task(conversation._tasks.close())
                await loop_turn()
                assert not close_job.done(), "关闭必须等待已接纳磁盘提交"
            # 2. 取消仍排空 worker，通知一次并撤权；晚到的首次 intent 整体回滚。
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await job
            with pytest.raises(asyncio.CancelledError):
                await effect
            if close_job is not None:
                await close_job
            if queued is not None:
                with pytest.raises(TaskServiceClosed):
                    await queued
            with pytest.raises(asyncio.CancelledError):
                await task.join()
            assert changed == [("conversation", 0), ("conversation", 1)]
            assert state.read("started") is None
            assert log.reader("s").get("first") == original
            assert [m.message_id for m in log.reader("s").snapshot()] == ["first", "second"]
            with pytest.raises(WriterExpired):
                outputs[0].append("old-output", Output((), "complete"))
            assert await conversation._inputs.append_async(
                "first", Input(()), on_commit=lambda _message, created: pytest.fail("replay was newly created") if created else None,
            ) == original
            with close_connection(sqlite3.connect(tmp_path / "sessions.db")) as raw:
                assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert not raw.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            gate.release.set()
            await asyncio.gather(*(cast(Awaitable[object], item) for item in (job, effect, queued, close_job) if item is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_stop_waiting_for_storage_preserves_reply_and_explicit_head(tmp_path, monkeypatch, automatic):
    """C3/C4：内部停止重选前缀，显式 head 不得被自动放宽，原回复保持完整。"""
    from plugins.ledger.log import MessageWriter, SourceHeadConflict
    waiting = asyncio.Event()
    release = asyncio.Event()
    answer_ready = asyncio.Event()
    finish = asyncio.Event()
    original = MessageWriter.append_async

    async def append(writer, message_id, body, **kwargs):
        if message_id == "stop":
            waiting.set()
            await release.wait()
        return await original(writer, message_id, body, **kwargs)

    async def run(task, reader, writer):
        answer_ready.set()
        await finish.wait()
        return writer.append("answer", Output((), "complete"),
                             expected_source_head=reader.head(source="conversation"))

    monkeypatch.setattr(MessageWriter, "append_async", append)
    async with source(tmp_path, run) as (conversation, log, _writer, program):
        first = await conversation.accept("input", Input(()))
        task = await conversation.start(program)
        assert task is not None
        await answer_ready.wait()
        operation = conversation.pause("stop") if automatic else conversation.control(
            "stop", Control("pause", first.seq), expected_head=first.seq, handle=task.handle,
        )
        job = asyncio.create_task(operation)
        try:
            await waiting.wait()
            finish.set()
            answer = await task.join()
            release.set()
            if automatic:
                stop = await job
                assert isinstance(stop.body, Control) and stop.body.through_seq == answer.seq
                assert [m.message_id for m in log.reader("s").snapshot()] == ["input", "answer", "stop"]
                assert await conversation.pause("stop") == stop
            else:
                with pytest.raises(SourceHeadConflict):
                    await job
                assert [m.message_id for m in log.reader("s").snapshot()] == ["input", "answer"]
            assert log.reader("s").get("input") == first
            assert log.reader("s").get("answer") == answer
        finally:
            finish.set()
            release.set()
            await asyncio.gather(job, return_exceptions=True)
