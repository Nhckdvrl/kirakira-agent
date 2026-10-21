"""Telegram previews must stay transient, bounded and independent of model generation."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telegram.error import NetworkError

from plugins.channels.contract import (
    ChannelFactoryContext,
    DeliveryStatus,
    ProviderDeliveryRequest,
)
from plugins.ledger.contract import ContentPart, Message, Output
from plugins.reply.contract import REPLY_STATUS, ReplyActivity, ReplyPreview
from plugins.telegram_channel.channel import TelegramChannelAdapter
from plugins.telegram_channel.drafts import DraftDeliveries, ReplyDrafts, draft_text
from plugins.telegram_sender.sender import TelegramSender


class Status:
    def __init__(self):
        self.items = {}
        self.changed = asyncio.Event()
        self.scopes = 0

    def snapshot(self, session):
        return self.items.get(session, ())

    def emit(self, message, text, *, chat=1, active=True, thinking="private reasoning"):
        self.items["telegram:" + str(chat)] = (ReplyActivity(
            "telegram:" + str(chat), "conversation", "task-" + message, active,
            ReplyPreview(message, text=text, thinking=thinking)),)
        self.changed.set()
        self.changed = asyncio.Event()

    async def follow(self, session):
        previous = None
        while True:
            changed = self.changed
            current = self.snapshot(session)
            if current != previous:
                previous = current
                yield tuple(asdict(item) for item in current)
            await changed.wait()

    @asynccontextmanager
    async def scope(self):
        self.scopes += 1
        owner = asyncio.current_task()

        @contextmanager
        def borrow(key):
            assert key == REPLY_STATUS and asyncio.current_task() is owner
            yield self

        try:
            yield SimpleNamespace(borrow=borrow)
        finally:
            self.scopes -= 1


class Bot:
    def __init__(self, *, blocked=False, fail=False):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.blocked, self.fail = blocked, fail

    async def send_message_draft(self, **values):
        self.entered.set()
        if self.blocked:
            await self.release.wait()
        if self.fail:
            raise NetworkError("controlled draft network failure")
        self.calls.append(("draft", values))
        return True

    async def send_message(self, **values):
        self.calls.append(("final", values))
        return SimpleNamespace(message_id=42)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_telegram_drafts_coalesce_without_backpressuring_generation_or_leaking_reasoning():
    status, bot = Status(), Bot(blocked=True)
    drafts = ReplyDrafts(SimpleNamespace(open_scope=status.scope), lambda: bot, interval=0.01)
    drafts.watch(1)
    try:
        status.emit("output", "first")
        await asyncio.wait_for(bot.entered.wait(), 2)
        # The model can publish its entire burst while the network is blocked.
        for number in range(100):
            status.emit("output", "latest-" + str(number))
        bot.release.set()
        await until(lambda: len(bot.calls) == 2)
        assert [call[1]["text"] for call in bot.calls] == ["first", "latest-99"]
        assert all("private reasoning" not in call[1]["text"] for call in bot.calls)
        assert len({call[1]["draft_id"] for call in bot.calls}) == 1
    finally:
        await drafts.close()
    assert status.scopes == 0


@pytest.mark.asyncio
async def test_telegram_final_delivery_drains_earlier_draft_and_suppresses_only_that_output():
    status, bot = Status(), Bot(blocked=True)
    drafts = ReplyDrafts(SimpleNamespace(open_scope=status.scope), lambda: bot, interval=0)
    drafts.watch(1)

    async def final():
        async with drafts.delivering(1, "old-output"):
            await bot.send_message(chat_id=1, text="complete")

    try:
        status.emit("old-output", "partial")
        await asyncio.wait_for(bot.entered.wait(), 2)
        task = asyncio.create_task(final())
        await asyncio.sleep(0)
        assert not task.done()
        bot.release.set()
        await asyncio.wait_for(task, 2)
        status.emit("old-output", "late stale partial")
        await asyncio.sleep(0)
        status.emit("new-output", "new turn")
        await until(lambda: len(bot.calls) == 3)
        assert [(kind, values["text"]) for kind, values in bot.calls] == [
            ("draft", "partial"), ("final", "complete"), ("draft", "new turn")]
    finally:
        await drafts.close()
    assert status.scopes == 0


@pytest.mark.asyncio
async def test_telegram_preview_failure_never_prevents_real_adapter_final_delivery():
    status, bot = Status(), Bot(fail=True)
    context = ChannelFactoryContext("snapshot", "generation", "boot", "binding", {},
        None, None, open_scope=status.scope)

    async def unused(_):
        raise AssertionError("no new credentials needed for an already started adapter")

    adapter = TelegramChannelAdapter(context, create_client=unused)
    adapter._started = True
    adapter._app = SimpleNamespace(bot=bot)
    adapter._drafts.watch(1)
    status.emit("output", "partial")
    await until(lambda: not adapter._drafts.tasks)
    receipt = await adapter.deliver(ProviderDeliveryRequest(
        "binding", "delivery", "1", "complete", session_message_id="output"))
    assert receipt.status == DeliveryStatus.DELIVERED and receipt.provider_ids == ("42",)
    assert bot.calls[-1][0] == "final"
    assert status.scopes == 0


@pytest.mark.asyncio
async def test_telegram_drafts_cancel_inflight_io_and_skip_groups():
    status, bot = Status(), Bot(blocked=True)
    drafts = ReplyDrafts(SimpleNamespace(open_scope=status.scope), lambda: bot)
    drafts.watch(-100)
    assert not drafts.tasks
    drafts.watch(1)
    status.emit("output", "partial")
    await asyncio.wait_for(bot.entered.wait(), 2)
    await asyncio.wait_for(drafts.close(), 2)
    assert not drafts.tasks and not bot.calls and status.scopes == 0
    assert draft_text("a" * 4095 + "😀") == "a" * 4095


@pytest.mark.asyncio
async def test_telegram_drafts_reopen_admission_while_old_scope_is_draining():
    status, bot = Status(), Bot(blocked=True)
    drafts = ReplyDrafts(SimpleNamespace(open_scope=status.scope), lambda: bot, interval=0)
    drafts.watch(1)
    status.emit("old", "old preview")
    await asyncio.wait_for(bot.entered.wait(), 2)
    drafts.pause()
    drafts.resume()
    bot.release.set()
    status.emit("new", "resumed preview")
    try:
        await until(lambda: any(call[1]["text"] == "resumed preview" for call in bot.calls))
    finally:
        await drafts.close()
    assert status.scopes == 0


@pytest.mark.asyncio
async def test_durable_telegram_sender_drains_preview_for_same_bot_only():
    status, bot = Status(), Bot(blocked=True)
    drafts = ReplyDrafts(SimpleNamespace(open_scope=status.scope), lambda: bot, interval=0)
    registry = DraftDeliveries()
    remove = registry.register(hashlib.sha256(b"fixture-token").hexdigest(), drafts)
    catalog = SimpleNamespace(reader=lambda _: SimpleNamespace(attachments=lambda _: ()))
    sender = TelegramSender(None, "https://fixture.invalid", "fixture-token", catalog, None,
                            draft_delivery=registry.delivering)

    async def post(request):
        bot.calls.append(("durable-final", request.fields))
        return 200, json.dumps({"ok": True, "result": {"message_id": 42}}).encode()

    sender._post = post
    message = Message("output", "telegram:1", 1, datetime.now(UTC), "assistant", "conversation",
                      Output((ContentPart("text", "complete"),), "complete"), {})
    drafts.watch(1)
    status.emit("output", "partial")
    await asyncio.wait_for(bot.entered.wait(), 2)
    try:
        async with registry.delivering("another-bot", 1, "unrelated-output"):
            pass
        pending = asyncio.create_task(sender.send("delivery", "1", message))
        await asyncio.sleep(0)
        assert not pending.done()
        bot.release.set()
        assert (await asyncio.wait_for(pending, 2)).status == "delivered"
        status.emit("output", "late text")
        await asyncio.sleep(0)
        assert [kind for kind, _ in bot.calls] == ["draft", "durable-final"]
    finally:
        await drafts.close()
        remove()
    assert status.scopes == 0 and not registry.readers


@pytest.mark.asyncio
async def test_telegram_subscription_is_cancelled_by_real_fiber_before_scope_drain(tmp_path):
    from agent.plugin_composition import CompositionRoot
    from agent.plugin_composition.requests import RequestContext

    root = CompositionRoot("preview-lifecycle")
    status, bot = Status(), Bot(blocked=True)

    async def provider(ctx):
        await ctx.provide(REPLY_STATUS, status)

    status_provider = await root.mount(provider, name="reply-provider")

    async def channel(ctx):
        @asynccontextmanager
        async def scope():
            async with ctx.runtime_scope():
                yield RequestContext("fixture", tmp_path, tmp_path, False, _borrow=ctx.borrow)

        drafts = ReplyDrafts(SimpleNamespace(open_scope=scope), lambda: bot, supervised=True)

        async def follow(child):
            drafts.resume()
            await child.spawn(drafts.run(), name="telegram-previews")

        await ctx.inject((REPLY_STATUS,), follow, name="reply-previews")
        drafts.watch(1)
        status.emit("output", "partial")

    await root.mount(channel, name="channel-owner")
    await asyncio.wait_for(bot.entered.wait(), 2)
    # Unload the borrowed provider first, as real generation disposal can do.
    await asyncio.wait_for(status_provider.dispose(), 2)
    assert not bot.calls
    status = Status()
    status.emit("replacement-output", "replacement reply")
    bot.release.set()
    await root.mount(provider, name="replacement-reply-provider")
    await until(lambda: bool(bot.calls))
    assert bot.calls[-1][1]["text"] == "replacement reply"
    await asyncio.wait_for(root.dispose(), 2)


@pytest.mark.asyncio
async def test_sender_reuses_a_lazy_connection_and_closes_it_with_its_owner():
    from agent.plugin_composition import CREDENTIALS, CredentialRef
    from plugins.delivery.contract import DELIVERY_SENDERS
    from plugins.ledger.contract import ARTIFACT_READ, MESSAGE_CATALOG
    from plugins.telegram_sender.plugin import run

    connections, requests, effects = [], [], []
    closed = asyncio.Event()

    async def handle(reader, writer):
        connections.append(writer)
        try:
            while True:
                try:
                    header = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    return
                length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                requests.append(await reader.readexactly(length))
                body = b'{"ok":true,"result":{"message_id":42}}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                             + b"\r\nContent-Type: application/json\r\n\r\n" + body)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    target = {}

    async def register(ctx, **values):
        target.update(values)

    @asynccontextmanager
    async def credentials(*_):
        yield SimpleNamespace(credential=lambda _: "fixture-token")

    @contextmanager
    def borrow(_):
        yield None

    async def effect(setup, **_):
        effects.append(setup())

    services = {DELIVERY_SENDERS: SimpleNamespace(register=register), CREDENTIALS: SimpleNamespace(open=credentials),
                MESSAGE_CATALOG: SimpleNamespace(reader=lambda _: SimpleNamespace(attachments=lambda _: ())),
                ARTIFACT_READ: None}
    ctx = SimpleNamespace(config={"enabled": True, "token": CredentialRef(("fixture",)),
                                  "api_base": f"http://127.0.0.1:{port}"},
                          require=services.__getitem__, borrow=borrow, effect=effect)
    try:
        await run(ctx)
        assert not connections  # Configuration and registration do not open sockets.
        for index in range(2):
            message = Message(f"output-{index}", "telegram:1", index, datetime.now(UTC), "assistant", "conversation",
                              Output((ContentPart("text", "complete"),), "complete"), {})
            async with target["open"]() as sender:
                assert (await sender.send(f"delivery-{index}", "1", message)).status == "delivered"
        assert len(requests) == 2 and len(connections) == 1
    finally:
        for cleanup in reversed(effects):
            await cleanup()
        server.close()
        await server.wait_closed()
    await asyncio.wait_for(closed.wait(), 2)
