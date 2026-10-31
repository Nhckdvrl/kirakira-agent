from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
import pytest
from plugins.ledger.durable_deliveries import TurnAcceptedReceipt
from plugins.channels.contract import (
    ChannelDeliveryReceipt,
    DeliveryStatus,
)
from plugins.ledger.durable_deliveries import (
    DurableBindingAttempt,
    DurableDeliveryRequest,
    PluginDurableDeliveries,
)
from plugins.ledger.durable_delivery_store import DurableDeliveryStore
from plugins.ledger.log import MessageLog
from plugins.ledger.contract import ContentPart, ContentReferences, Output

def _request(logical_id: str = "delivery:one") -> DurableDeliveryRequest:
    return DurableDeliveryRequest(
        logical_delivery_id=logical_id,
        accepted_turn=TurnAcceptedReceipt("wake:default", "turn:one"),
        target_service="eventmail.delivery.v1",
        channel="recording",
        recipient="recipient:one",
        projection_session_id="recipient-session",
        body="hello from Wake",
        metadata={"proactive": True},
    )

@pytest.mark.asyncio
async def test_provider_receipt_precedes_one_append_only_session_projection(
    tmp_path: Path,
) -> None:
    store = DurableDeliveryStore(tmp_path / "settlements.sqlite")
    log = MessageLog(tmp_path / "sessions.db")
    provider_states: list[str] = []

    async def sender(request, provider_started):
        provider_started(
            DurableBindingAttempt(
                request.logical_delivery_id,
                "snapshot:one",
                "generation:one",
                "binding:one",
            )
        )
        row = store.lookup("wake:default", "turn:one")
        assert row is not None
        provider_states.append(str(row["state"]))
        return ChannelDeliveryReceipt(
            request.logical_delivery_id,
            DeliveryStatus.DELIVERED,
            ("provider:one",),
        )

    async def project(request) -> str:
        row = store.lookup("wake:default", "turn:one")
        assert row is not None and row["state"] == "delivered"
        writer = log.writer(
            request.projection_session_id,
            author="assistant", source="delivery", body_types=(Output,),
            content={"text": lambda _: ContentReferences()},
            message_metadata_keys=frozenset({"delivery"}),
        )
        return writer.append(
            request.logical_delivery_id,
            Output((ContentPart("text", request.body),), "complete"),
            metadata={"delivery": {"id": request.logical_delivery_id}},
        ).message_id

    service = PluginDurableDeliveries(store, sender, project)
    projected = await service.submit(_request())
    duplicate = await service.submit(_request())

    assert provider_states == ["provider_started"]
    assert projected.state == duplicate.state == "projected"
    messages = log.reader("recipient-session").snapshot()
    assert len(messages) == 1
    assert messages[0].body == Output((ContentPart("text", "hello from Wake"),), "complete")
    assert messages[0].metadata["delivery"] == {"id": "delivery:one"}
    assert store.confirm_settled("delivery:one", "domain:one")["state"] == "settled"
    assert store.confirm_settled("delivery:one", "domain:one")["state"] == "settled"
    log.close()

def test_provider_started_sigkill_recovers_uncertain_without_resend(
    tmp_path: Path,
) -> None:
    runner = Path(__file__).parent / "fixtures" / "durable_delivery_crash" / "runner.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    completed = subprocess.run(
        [sys.executable, str(runner), str(tmp_path)],
        env=env,
        check=False,
    )
    assert completed.returncode == -9
    assert (tmp_path / "provider-edge").read_text(encoding="utf-8").splitlines() == [
        "provider_started"
    ]
    assert not (tmp_path / "provider-calls").exists()

    store = DurableDeliveryStore(tmp_path / "settlements.sqlite")
    service = PluginDurableDeliveries(store, None, None)
    recovered = service.lookup(TurnAcceptedReceipt("session:crash", "turn:crash"))
    assert recovered is not None and recovered.state == "failed"
    assert recovered.provider_receipt == {"status": "failed", "error": "provider call interrupted; delivery may have occurred"}
    assert service.recoverable() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_boundary", [1, 2, 3])
async def test_native_delivery_read_cancellation_drains_without_sending_twice(tmp_path, monkeypatch, read_boundary):
    import asyncio
    import threading
    from contextlib import asynccontextmanager
    from agent.plugin_composition.tasks import Tasks
    from plugins.delivery.api import Receipt
    from plugins.delivery.execution import Deliveries
    from plugins.delivery.records import DeliveryRecords
    from tests.test_akasha_execution import WorkerGate, loop_turn
    from tests.test_message_log import writer

    log, tasks = MessageLog(tmp_path / "native.db"), Tasks()
    sent = []
    loop_thread = threading.get_ident()

    class Sender:
        idempotent = True
        async def send(self, key, address, message):
            assert threading.get_ident() == loop_thread
            sent.append((key, message.message_id))
            return Receipt(status="delivered")
        async def query(self, key, address):
            return None

    @asynccontextmanager
    async def open_sender(binding):
        assert threading.get_ident() == loop_thread
        yield Sender()

    records = DeliveryRecords(log.owner("delivery"), "policy")
    service = Deliveries(records, log.catalog(), tasks, open_sender, task_key="native")
    log.save_binding("sender", {"fixture": True})
    message = writer(log, author="assistant", bodies=(Output,)).append("one", Output((), "complete"))
    await service.prepare_async(log.reader("s"), message, ({"name":"test", "binding_id":"sender", "address":"room"},), passive=True)
    original, gate, reads = records.read, WorkerGate(), 0

    def gated_read(message_id, sink):
        nonlocal reads
        if threading.get_ident() == loop_thread:
            return original(message_id, sink)  # _start keeps its intentional synchronous admission boundary.
        reads += 1
        if reads == read_boundary:
            with log._read_admission:
                gate.stop()
        return original(message_id, sink)

    monkeypatch.setattr(records, "read", gated_read)
    pending = asyncio.create_task(service.send("one", "test"))
    try:
        await gate.wait(pending)
        await loop_turn()
        pending.cancel()
        await loop_turn()
        pending.cancel()
        await loop_turn()
        assert not pending.done() and not sent
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert original("one", "test")[1].phase == "prepared"
        # Two actual callers can resume the prepared effect, while Task admission
        # and the durable receipt still permit only one sender invocation.
        receipts = await asyncio.gather(service.send("one", "test"), service.send("one", "test"))
        assert all(receipt.status == "delivered" for receipt in receipts)
        assert len(sent) == 1 and original("one", "test")[1].phase == "delivered"
    finally:
        gate.release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await tasks.close()
        log.close()


@pytest.mark.asyncio
async def test_native_delivery_rechecks_sender_registration_after_read_and_retry_drains(tmp_path, monkeypatch):
    import asyncio
    import threading
    from contextlib import asynccontextmanager
    from agent.plugin_composition import CompositionRoot
    from agent.plugin_composition.model import PluginRuntime
    from agent.plugin_composition.tasks import Tasks
    from plugins.delivery.api import Receipt
    from plugins.delivery.execution import Deliveries
    from plugins.delivery.records import Delivery, DeliveryRecords
    from plugins.delivery.senders import Senders
    from tests.test_akasha_execution import WorkerGate, loop_turn
    from tests.test_message_log import writer

    log, root, tasks = MessageLog(tmp_path / "owner.db"), CompositionRoot("delivery-owner"), Tasks()
    senders, sent = Senders(root.context), []
    loop_thread = threading.get_ident()

    class Sender:
        idempotent = True
        async def send(self, key, address, message):
            sent.append(message.message_id)
            return Receipt(status="delivered")
        async def query(self, key, address):
            return None

    @asynccontextmanager
    async def target():
        assert threading.get_ident() == loop_thread
        yield Sender()

    contexts, effects = [], []
    async def owner(ctx):
        contexts.append(ctx)
        effects.append(await senders.register(ctx, name="test", idempotent=True, open=target))
    await root.mount(owner, name="sender-owner", runtime=PluginRuntime(
        "sender-owner", "delivery-owner", tmp_path, tmp_path, tmp_path, {}))
    owner_context, effect = contexts[0], effects[0]
    metadata = {"name":"test", "owner":owner_context.runtime.plugin_id, "idempotent":True}

    @asynccontextmanager
    async def open_sender(binding):
        assert threading.get_ident() == loop_thread
        async with senders.open(metadata) as sender:
            yield sender

    records = DeliveryRecords(log.owner("delivery"), "policy")
    service = Deliveries(records, log.catalog(), tasks, open_sender, task_key="native")
    log.save_binding("sender", {"fixture": True})
    message = writer(log, author="assistant", bodies=(Output,)).append("one", Output((), "complete"))
    await service.prepare_async(log.reader("s"), message, ({"name":"test", "binding_id":"sender", "address":"room"},))
    original, gate, reads, blocking_read = records.read, WorkerGate(), 0, 2

    def gated_read(message_id, sink):
        nonlocal reads
        if threading.get_ident() == loop_thread:
            return original(message_id, sink)
        reads += 1
        if reads == blocking_read:
            with log._read_admission:
                gate.stop()
        return original(message_id, sink)

    monkeypatch.setattr(records, "read", gated_read)
    pending = asyncio.create_task(service.send("one", "test"))
    try:
        await gate.wait(pending)
        await loop_turn()
        await effect.aclose()  # Withdraw the actual sender registration while physical SQL is pending.
        gate.release.set()
        with pytest.raises(KeyError):
            await pending
        assert not sent and original("one", "test")[1].phase == "prepared"
        async with owner_context.runtime_scope():
            effect = await senders.register(owner_context, name="test", idempotent=True, open=target)

        record, delivery = original("one", "test")
        await records.save_async("one", record, Delivery(sink=delivery.sink, phase="rejected", receipt=Receipt(status="rejected")))
        gate, reads, blocking_read = WorkerGate(), 0, 1
        pending = asyncio.create_task(service.retry("one", "test"))
        await gate.wait(pending)
        pending.cancel()
        await loop_turn()
        pending.cancel()
        await loop_turn()
        assert not pending.done() and not sent
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert original("one", "test")[1].phase == "rejected"
        assert (await service.retry("one", "test")).status == "delivered"
        assert sent == ["one"]
    finally:
        gate.release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await tasks.close()
        await root.dispose()
        log.close()
