from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from functools import partial
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import (
    RUNTIME_STARTED,
    RUNTIME_STARTING,
    RUNTIME_STOPPING,
    Context,
    stop_spawned,
)
from agent.plugin_composition.tasks import ExternalRootPermit, RestartRejectedError
from core.common.file_io import run_file_io
from plugins.conversation.contract import CHECK_ORIGIN, OriginCheck
from plugins.delivery.contract import (
    DELIVERY_GUARDED_START,
    DELIVERY_SENDERS,
    FINAL_OUTPUT_DELIVERY,
    FinalOutputTurn,
    GuardedDeliveries,
)
from plugins.delivery_policy.contract import (
    INPUT_ORIGIN as INPUT_ORIGIN,
)
from plugins.ledger.contract import (
    BINDINGS,
    MESSAGE_CATALOG,
    ContentPart,
    Control,
    Input,
    Message,
    MessageReader,
    Output,
    ToolCall,
)
from plugins.reply.contract import REPLY_COMPLETION

from .follow import follow

logger = logging.getLogger(__name__)

api_version = 3
name = "delivery_policy"
version = "1.0.0"
desc = "默认只发送完整可见回复；显式通知沿来源自己的固定发送选择"
inject = (
    DELIVERY_GUARDED_START,
    DELIVERY_SENDERS,
    BINDINGS,
    MESSAGE_CATALOG,
    FINAL_OUTPUT_DELIVERY,
    CHECK_ORIGIN,
)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: tuple[str, ...] = Field(default=("conversation",), min_length=1)


def origin(
    reader: MessageReader,
    message: Message,
    sources: tuple[str, ...],
    *,
    check_origin: OriginCheck | None = None,
) -> tuple[str, str] | None:
    """默认回复发往其消息前缀中最后一次同来源输入；后来消息不改变旧目的地。"""
    if reader.attributes.visibility == "internal":
        return None
    body = message.body
    if message.source in sources and isinstance(body, Control) and (
        body.action == "failure"
        or body.action == "abandon" and (body.reason or "").startswith("步数上限放弃")
    ):
        latest = reader.latest_input(message.source, through_seq=reader.head())
        # A newer input supersedes an old failure. Recovery must not flood a
        # channel with historical errors or redirect them to a later recipient.
        if latest is None or latest.seq > body.through_seq:
            return None
        return input_origin(reader, message.source, through_seq=body.through_seq,
                            check_origin=check_origin)
    if (message.source not in sources or not isinstance(body, Output) or body.finish != "complete"
            or any(isinstance(part, ToolCall) for part in body.parts)):
        return None
    visible = any(isinstance(part, ContentPart) and (
        part.kind == "artifact_ref" or part.kind == "text" and isinstance(part.value, str) and part.value.strip()
    ) for part in body.parts)
    if not visible:
        return None
    return input_origin(
        reader,
        message.source,
        through_seq=message.seq,
        check_origin=check_origin,
    )


def input_origin(
    reader: MessageReader,
    source: str,
    *,
    through_seq: int,
    check_origin: OriginCheck | None = None,
) -> tuple[str, str] | None:
    """只从原输入的已验证渠道事实读取目的地。"""
    previous = reader.latest_input(source, through_seq=through_seq)
    if previous is None:
        return None
    assert isinstance(previous.body, Input)
    parts = [part for part in previous.body.parts if part.kind == "channel.origin"]
    if not parts:
        return None
    if len(parts) != 1:
        raise ValueError("输入必须只有一个渠道来源")
    if check_origin is not None:
        _ = check_origin(parts[0])
    value = cast(Mapping[str, str], parts[0].value)
    return value["channel"], value["chat_id"]


class DeliveryFinalOutput:
    """等待 delivery owner 为一个最终 Output 写入 delivered 回执。"""

    def __init__(self, ctx: Context, delivery: Callable[[], GuardedDeliveries], timeout_s: float = 15.0) -> None:
        self._ctx = ctx
        self._delivery = delivery
        self._timeout_s = timeout_s
        self.wait = ctx.entrypoint(self.wait)

    async def wait(self, reader: MessageReader, turn: FinalOutputTurn) -> None:
        ending = turn.ending_message_id
        if ending is None:
            raise RestartRejectedError("最终 Turn 没有 Output")
        delivery = self._delivery()
        async with asyncio.timeout(self._timeout_s):
            # ReplyCompletion 在 run_reply 的资源 cleanup 之后才创建首次选路；
            # 日志 follower 可能先看到 complete Output，先等待 Delivery owner 的真实 selection。
            selection = await run_file_io(lambda: delivery.selection(ending))
            while selection is None:
                await asyncio.sleep(0.01)
                selection = await run_file_io(lambda: delivery.selection(ending))
            if not selection.sinks:
                raise RestartRejectedError("最终 Output 没有 delivery provider")
            while True:
                receipts = await run_file_io(lambda: tuple(delivery.receipt(ending, sink) for sink in selection.sinks))
                if any(receipt is not None and receipt.status in {"failed", "rejected"} for receipt in receipts):
                    raise RestartRejectedError("最终 Output delivery 未确认")
                if len(receipts) == len(selection.sinks) and all(
                    receipt is not None and receipt.status == "delivered" for receipt in receipts
                ):
                    return
                await asyncio.sleep(0.01)


async def apply(ctx: Context) -> None:
    """正式启动后跟随日志；不把策略、学习或来源 ACK 放进发送原子能力。"""
    config = Config.model_validate(ctx.config)
    delivery: GuardedDeliveries | None = None

    def current_delivery() -> GuardedDeliveries:
        if delivery is None:
            raise RuntimeError("Delivery 尚未完成正式启动准备")
        return delivery

    final_delivery = DeliveryFinalOutput(ctx, current_delivery)
    origin_check = ctx.require(CHECK_ORIGIN)
    _ = await ctx.provide(INPUT_ORIGIN, partial(input_origin, check_origin=origin_check))
    final_outputs = ctx.require(FINAL_OUTPUT_DELIVERY)

    def register_final_outputs() -> Callable[[], None]:
        for source in config.sources:
            final_outputs.register(source, final_delivery)

        def unregister_final_outputs() -> None:
            for source in config.sources:
                final_outputs.unregister(source, final_delivery)

        return unregister_final_outputs

    _ = await ctx.effect(register_final_outputs, label="delivery-policy-final-output")
    watcher: asyncio.Task[None] | None = None
    recovery: dict[tuple[str, str], AbstractContextManager[None]] = {}

    def settled(message_id: str, sink: str) -> None:
        hold = recovery.pop((message_id, sink), None)
        if hold is not None:
            _ = hold.__exit__(None, None, None)

    def close_recovery() -> None:
        for hold in recovery.values():
            _ = hold.__exit__(None, None, None)
        recovery.clear()

    async def prepare(_event: object) -> None:
        """正式接纳开放前占住原被动效果，直到 follower 实际恢复一次发送或查询。"""
        nonlocal delivery
        delivery = ctx.require(DELIVERY_GUARDED_START).open(ctx)
        selected_delivery = delivery
        def pending_targets():
            targets = []
            for message_id, sink in selected_delivery.pending():
                selection = selected_delivery.selection(message_id)
                assert selection is not None
                if selection.passive:
                    targets.append((message_id, sink, selected_delivery.destination(message_id, sink)))
            return tuple(targets)
        for message_id, sink, target in await run_file_io(pending_targets):
            if (message_id, sink) not in recovery:
                hold = selected_delivery.activity(target.name, target.address)
                _ = hold.__enter__()
                recovery[message_id, sink] = hold

    _ = await ctx.effect(lambda: close_recovery, label="pending-delivery")

    async def select(reader: MessageReader, message: Message) -> tuple[Mapping[str, object], ...] | None:
        if message.source not in config.sources:
            return None
        def read_origin(view: MessageReader):
            return (view.attributes.visibility != "internal",
                    origin(view, message, config.sources, check_origin=origin_check))
        eligible, route = await reader.read_async(read_origin)
        if not eligible:
            return None
        if route is None:
            return ()
        name, address = route
        binding = ctx.require(DELIVERY_SENDERS).bind(name, ctx.require(BINDINGS))
        return ({"name": name, "binding_id": binding, "address": address},)

    class ReplyCompletion:
        def activity(self, reader: MessageReader, source: str) -> AbstractContextManager[None]:
            route = (input_origin(reader, source, through_seq=reader.head(), check_origin=origin_check)
                     if source in config.sources and reader.attributes.visibility != "internal" else None)
            return nullcontext() if route is None else current_delivery().activity(*route)

        @asynccontextmanager
        async def __call__(
            self,
            reader: MessageReader,
            source: str,
            *,
            child_permit: Callable[[], ExternalRootPermit] | None = None,
        ) -> AsyncGenerator[None]:
            """完成后固定选路并独立启动发送，新 Input 不取得旧发送的取消权。"""
            def read_activity(view: MessageReader):
                head = view.head()
                route = (input_origin(view, source, through_seq=head, check_origin=origin_check)
                         if source in config.sources and view.attributes.visibility != "internal" else None)
                return head, route
            head, route = await reader.read_async(read_activity)
            delivery = current_delivery()
            with nullcontext() if route is None else delivery.activity(*route):
                try:
                    yield
                finally:
                    def read_completed(view: MessageReader):
                        return tuple((message, delivery.selection(message.message_id) is not None)
                                     for message in view.snapshot(after_seq=head)
                                     if message.source == source and origin(
                                         view, message, config.sources, check_origin=origin_check) is not None)
                    completed = await reader.read_async(read_completed)
                    for message, known in completed:
                        try:
                            if not known:
                                async with ctx.runtime_scope():
                                    sinks = await select(reader, message)
                            else:
                                sinks = ()
                            assert sinks is not None
                            selected = await delivery.prepare_async(reader, message, sinks, passive=True)
                            for sink in selected.sinks:
                                permit = None if child_permit is None else child_permit()
                                try:
                                    task = await delivery.start(message.message_id, sink)
                                except BaseException:
                                    if permit is not None:
                                        permit.release()
                                    raise
                                if permit is not None:
                                    task.on_done(permit.release)
                        except Exception:
                            # 回复事实已提交；发送失败不能反向改写来源为推理失败。
                            logger.exception("回复发送未结算，保留原效果 message=%s", message.message_id)

    _ = await ctx.provide(REPLY_COMPLETION, ReplyCompletion())

    async def start(_event: object) -> None:
        nonlocal watcher
        watcher = await ctx.spawn(follow(
            ctx, ctx.require(MESSAGE_CATALOG), current_delivery, select, settled=settled,
        ), name="delivery")

    async def stop(_event: object) -> None:
        try:
            await stop_spawned(watcher)
        finally:
            close_recovery()

    _ = await ctx.on(RUNTIME_STARTING, prepare)
    _ = await ctx.on(RUNTIME_STARTED, start)
    _ = await ctx.on(RUNTIME_STOPPING, stop)
