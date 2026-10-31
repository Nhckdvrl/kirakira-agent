import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plugins.channels.contract import CHANNEL_INPUT_V2 as CHANNEL_INPUT
from plugins.channels.contract import ChannelInboundMessage
from plugins.delivery.records import DeliveryRecords
from plugins.ledger.contract import Output, OwnerRecord
from tests.support.delivery_sources import sources
from tests.test_default_reply import application, live_root


@pytest.mark.asyncio
async def test_failure_control_wakes_durable_delivery_without_exposing_exception(tmp_path):
    from contextlib import asynccontextmanager
    from agent.plugin_composition import CompositionRoot
    from agent.plugin_composition.tasks import Tasks
    from plugins.delivery.api import Receipt
    from plugins.delivery.execution import Deliveries
    from plugins.delivery_policy.follow import follow
    from plugins.delivery_policy.plugin import origin
    from plugins.ledger.contract import ContentPart, ContentReferences, Control, Input, SessionAttributes
    from plugins.ledger.log import MessageLog
    from plugins.telegram_sender.sender import read_content

    log = MessageLog(tmp_path / "failures.db")
    log.ensure_session("test:room", SessionAttributes())
    inputs = log.writer("test:room", author="user", source="conversation", body_types=(Input,),
                        content={"text": lambda _: ContentReferences(), "channel.origin": lambda _: ContentReferences()})
    controls = log.writer("test:room", author="app", source="conversation", body_types=(Control,), content={})
    first = inputs.append("first", Input((ContentPart("text", "hello"), ContentPart(
        "channel.origin", {"channel": "test", "chat_id": "original"}))))
    root, tasks, sent = CompositionRoot("failure-delivery"), Tasks(), []
    done = asyncio.Event()

    class Sender:
        idempotent = True
        async def send(self, key, address, message):
            parts = await read_content(message, log.catalog(), None)
            sent.append((message.message_id, address, parts))
            done.set()
            return Receipt(status="delivered")
        async def query(self, key, address):
            return None

    @asynccontextmanager
    async def open_sender(_):
        yield Sender()

    log.save_binding("sender", {"fixture": True})
    records = DeliveryRecords(log.owner("delivery"), "policy")
    delivery = Deliveries(records, log.catalog(), tasks, open_sender, task_key="failure-delivery")
    def select(reader, message):
        route = origin(reader, message, ("conversation",))
        return () if route is None else ({"name": "test", "binding_id": "sender", "address": route[1]},)
    job = asyncio.create_task(follow(root.context, log.catalog(), lambda: delivery, select))
    try:
        await asyncio.sleep(0)
        failed = controls.append("failure", Control("failure", first.seq,
            "模型服务拒绝请求（HTTP 451）。secret-key-fixture raw provider payload"))
        await asyncio.wait_for(done.wait(), 3)
        async with asyncio.timeout(3):
            while records.read(failed.message_id, "test")[1].phase != "delivered":
                await asyncio.sleep(0.01)
        assert sent[0][0:2] == ("failure", "original")
        assert "HTTP 451" in sent[0][2][0] and "secret-key-fixture" not in sent[0][2][0]
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        job = asyncio.create_task(follow(root.context, log.catalog(), lambda: delivery, select))
        await asyncio.sleep(0.05)
        assert len(sent) == 1
        inputs.append("next", Input((ContentPart("text", "new question"), ContentPart(
            "channel.origin", {"channel": "test", "chat_id": "later"}))))
        assert origin(log.reader("test:room"), failed, ("conversation",)) is None
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await tasks.close()
        await root.dispose()
        log.close()


@pytest.mark.asyncio
async def test_real_input_reply_and_archived_delivery_are_independent_consumers(tmp_path, monkeypatch):
    sources(tmp_path / "plugins")
    shutil.copytree(Path(__file__).parents[1] / "plugins/delivery_policy", tmp_path / "plugins/delivery_policy",
                    ignore=shutil.ignore_patterns("__pycache__"))
    delivered = asyncio.Event()
    async with application(tmp_path, replying=True) as (log, host):
        store_type = type(log.owner("plugin:delivery"))
        original = store_type.transact_async

        async def observe(self, callback, **kwargs):
            result = await original(self, callback, **kwargs)
            if isinstance(result, OwnerRecord) and result.value.get("phase") == "delivered":
                delivered.set()
            return result

        monkeypatch.setattr(store_type, "transact_async", observe)
        async with live_root(host) as root:
            accepted = await root.context.require(CHANNEL_INPUT)(
                "test:room", "u1", ChannelInboundMessage(
                    "test", "user", "room", "do the work", datetime(2026, 9, 6, tzinfo=UTC), {},
                ),
            )
        async with asyncio.timeout(10):
            await delivered.wait()
        messages = log.reader("test:room").snapshot()
        assert messages[0] == accepted
        final = [message for message in messages if isinstance(message.body, Output) and message.body.finish == "complete"]
        assert len(final) == 1
        import json
        effects = [json.loads(line) for line in next((tmp_path / "workspace").rglob("sent.jsonl")).read_text().splitlines()]
        assert len(effects) == 1
        assert effects[0][1:3] == ["room", final[0].message_id]
        records = DeliveryRecords(log.owner("plugin:delivery"), "delivery_policy")
        assert records.read(final[0].message_id, "test")[1].phase == "delivered"
        # Settlement and the independent archived-message consumer commit separately.
        # Use a fresh reader: its cursor cache intentionally does not follow other readers.
        async with asyncio.timeout(10):
            while DeliveryRecords(log.owner("plugin:delivery"), "delivery_policy").cursor("test:room") != final[0].seq:
                await asyncio.sleep(0.01)
        from plugins.delivery.history import DeliveryHistory
        history = DeliveryHistory(lambda: log.owner("plugin:delivery"), log.catalog())
        confirmation = history.confirmed(final[0].message_id)
        assert confirmation is not None and confirmation.message == final[0]
        assert history.confirmed("not-delivered") is None
        assert history.recent(since=accepted.recorded_at, until=datetime.now(UTC), limit=10)[0] == confirmation


@pytest.mark.asyncio
async def test_slow_destination_does_not_delay_next_fast_receipt(tmp_path, monkeypatch):
    """O: real durable selections advance independently, with per-sink order."""
    from contextlib import asynccontextmanager

    from agent.plugin_composition import CompositionRoot
    from agent.plugin_composition.tasks import Tasks
    from plugins.delivery.api import Receipt
    from plugins.delivery.execution import Deliveries
    from plugins.delivery_policy.follow import follow
    from plugins.delivery_policy.plugin import DeliveryFinalOutput
    from plugins.ledger.log import MessageLog
    from tests.test_akasha_execution import WorkerGate, loop_turn
    from tests.test_message_log import writer
    import threading
    from types import SimpleNamespace

    log = MessageLog(tmp_path / "sessions.db")
    root = CompositionRoot("delivery-isolation")
    tasks = Tasks()
    entered, release, fast, slow = (asyncio.Event() for _ in range(4))
    sent = {"slow": [], "fast": []}

    class Sender:
        idempotent = True

        async def send(self, key, address, message):
            if address == "slow" and message.message_id == "first":
                entered.set()
                await release.wait()
            sent[address].append(message.message_id)
            return Receipt(status="delivered")

        async def query(self, key, address):
            return None

    @asynccontextmanager
    async def open_sender(binding):
        yield Sender()

    records = DeliveryRecords(log.owner("delivery"), "policy")
    delivery = Deliveries(records, log.catalog(), tasks, open_sender, task_key="delivery")
    loop_thread = threading.get_ident()
    final_gate = None

    class CheckedDelivery:
        """只校验已获授权的纯读离开 loop；发送与 Task 权限仍用原服务。"""
        def __getattr__(self, name):
            operation = getattr(delivery, name)
            if name not in {"cursor", "selection", "pending", "receipt"}:
                return operation
            def read(*args, **kwargs):
                assert threading.get_ident() != loop_thread, f"delivery {name} blocked the loop"
                if final_gate is not None and name == "selection":
                    with log._read_admission:
                        final_gate.stop()
                return operation(*args, **kwargs)
            return read

    checked = CheckedDelivery()
    def current_delivery():
        assert threading.get_ident() == loop_thread, "delivery authority must be obtained on the loop"
        return checked
    sinks = tuple({"name": name, "binding_id": name, "address": name} for name in sent)
    for name in sent:
        log.save_binding(name, {"name": name})
    outputs = writer(log, author="assistant", bodies=(Output,))
    first = outputs.append("first", Output((), "complete"))
    def select(reader, message):
        assert threading.get_ident() == loop_thread, "selection authority must stay on the loop"
        assert message.message_id != "first", "prepared recovery must keep its original selection"
        return sinks
    def settled(message_id, sink):
        if sink == "fast" and message_id == "second":
            fast.set()
        if sink == "slow" and message_id == "second":
            slow.set()

    gate = WorkerGate()
    original_read = type(log.reader("s")).read
    stopped = False
    def read(reader, **options):
        nonlocal stopped
        if options.get("limit") == 100:
            assert threading.get_ident() != loop_thread, "delivery backlog blocked the loop"
            if not stopped:
                stopped = True
                with log._read_admission:
                    gate.stop()
        return original_read(reader, **options)
    monkeypatch.setattr(type(log.reader("s")), "read", read)
    job = asyncio.create_task(follow(root.context, log.catalog(), current_delivery,
                                    select, settled=settled))
    try:
        # 真实目录读取持锁时，取消不能提前释放 owner，也不能冻结其他 loop 工作。
        await gate.wait(job)
        await loop_turn()
        job.cancel()
        await loop_turn()
        job.cancel()
        await loop_turn()
        assert not job.done() and sent == {"slow": [], "fast": []}
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert records.selection("first") is None and records.cursor("s") == -1
        prepared = await delivery.prepare_async(log.reader("s"), first, sinks, passive=True)
        job = asyncio.create_task(follow(root.context, log.catalog(), current_delivery,
                                        select, settled=settled))
        await asyncio.wait_for(entered.wait(), 2)
        outputs.append("second", Output((), "complete"))
        await asyncio.wait_for(fast.wait(), 2)
        assert records.read("second", "fast")[1].phase == "delivered"
        assert records.read("first", "slow")[1].phase == "started"
        assert sent["fast"] == ["first", "second"]
        release.set()
        # Joining the original effect also observes its durable settlement.
        await delivery.send("first", "slow")
        await asyncio.wait_for(slow.wait(), 2)
        assert sent["slow"] == ["first", "second"] and sent["fast"] == ["first", "second"]
        assert records.selection("first") == prepared

        # FinalOutput 的纯回执轮询同样可排空；取消不改变任何真实投递回执。
        final_gate = WorkerGate()
        waiter = DeliveryFinalOutput(root.context, current_delivery)
        waiting = asyncio.create_task(waiter.wait(log.reader("s"), SimpleNamespace(ending_message_id="second")))
        try:
            await final_gate.wait(waiting)
            await loop_turn()
            waiting.cancel()
            await loop_turn()
            waiting.cancel()
            await loop_turn()
            assert not waiting.done()
            final_gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        finally:
            final_gate.release.set()
            await asyncio.gather(waiting, return_exceptions=True)
            final_gate = None
        await waiter.wait(log.reader("s"), SimpleNamespace(ending_message_id="second"))
        assert sent == {"slow": ["first", "second"], "fast": ["first", "second"]}
    finally:
        gate.release.set()
        release.set()
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await tasks.close()
        await root.dispose()
        log.close()
