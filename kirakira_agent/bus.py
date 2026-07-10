"""Async message bus and per-chat send ordering."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, TypeVar

from kirakira_agent.events import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass
class _LaneState:
    condition: asyncio.Condition
    passive_turns: int = 0
    passive_sends: int = 0
    sending: bool = False
    next_ticket: int = 0
    serving_ticket: int = 0


class ChatLane:
    """Coordinates passive turns and outbound sends for the same chat."""

    def __init__(self) -> None:
        self._states: Dict[Tuple[str, str], _LaneState] = {}

    def _state(self, channel: str, chat_id: str) -> _LaneState:
        key = (str(channel), str(chat_id))
        state = self._states.get(key)
        if state is None:
            state = _LaneState(condition=asyncio.Condition())
            self._states[key] = state
        return state

    async def mark_passive_pending(self, channel: str, chat_id: str) -> None:
        state = self._state(channel, chat_id)
        async with state.condition:
            state.passive_turns += 1
            state.condition.notify_all()

    async def mark_passive_done(self, channel: str, chat_id: str) -> None:
        state = self._state(channel, chat_id)
        async with state.condition:
            state.passive_turns = max(0, state.passive_turns - 1)
            state.condition.notify_all()

    async def mark_passive_send_pending(self, channel: str, chat_id: str) -> int:
        state = self._state(channel, chat_id)
        async with state.condition:
            ticket = state.next_ticket
            state.next_ticket += 1
            state.passive_sends += 1
            state.condition.notify_all()
            return ticket

    async def run_passive(
        self,
        channel: str,
        chat_id: str,
        ticket: int,
        send: Callable[[], Awaitable[_T]],
    ) -> _T:
        state = self._state(channel, chat_id)
        async with state.condition:
            while state.sending or ticket != state.serving_ticket:
                await state.condition.wait()
            state.sending = True
        try:
            return await send()
        finally:
            async with state.condition:
                state.passive_sends = max(0, state.passive_sends - 1)
                state.serving_ticket += 1
                state.sending = False
                state.condition.notify_all()


class MessageBus:
    def __init__(self, chat_lane: ChatLane | None = None) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound: asyncio.Queue[tuple[OutboundMessage, int]] = asyncio.Queue()
        self._subscribers: Dict[str, List[Callable[[OutboundMessage], Awaitable[None]]]] = {}
        self._chat_lane = chat_lane or ChatLane()
        self._running = False
        self._dispatch_tasks: set[asyncio.Task[None]] = set()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        await self._chat_lane.mark_passive_pending(msg.channel, msg.chat_id)
        await self._inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        return await self._inbound.get()

    async def complete_inbound(self, msg: InboundMessage) -> None:
        await self._chat_lane.mark_passive_done(msg.channel, msg.chat_id)
        self._inbound.task_done()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        ticket = await self._chat_lane.mark_passive_send_pending(
            msg.channel, msg.chat_id
        )
        await self._outbound.put((msg, ticket))

    def subscribe_outbound(
        self,
        channel: str,
        callback: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        subscribers = self._subscribers.setdefault(channel, [])
        if callback not in subscribers:
            subscribers.append(callback)

    def unsubscribe_outbound(
        self,
        channel: str,
        callback: Callable[[OutboundMessage], Awaitable[None]],
    ) -> None:
        subscribers = self._subscribers.get(channel, [])
        if callback in subscribers:
            subscribers.remove(callback)
        if not subscribers:
            self._subscribers.pop(channel, None)

    async def dispatch_outbound(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    msg, ticket = await asyncio.wait_for(
                        self._outbound.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                task = asyncio.create_task(
                    self._dispatch_one(msg, ticket),
                    name="outbound:%s:%s" % (msg.channel, msg.chat_id),
                )
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)
        finally:
            if self._dispatch_tasks:
                await asyncio.gather(*list(self._dispatch_tasks), return_exceptions=True)

    async def _dispatch_one(self, msg: OutboundMessage, ticket: int) -> None:
        try:
            await self._chat_lane.run_passive(
                msg.channel,
                msg.chat_id,
                ticket,
                lambda: self._send_outbound(msg),
            )
        finally:
            self._outbound.task_done()

    async def _send_outbound(self, msg: OutboundMessage) -> None:
        subscribers = list(self._subscribers.get(msg.channel, []))
        if not subscribers:
            logger.warning("no outbound subscriber for channel=%s", msg.channel)
            return
        for cb in subscribers:
            try:
                await cb(msg)
            except Exception as exc:
                logger.warning("outbound dispatch failed once: %s", exc)
                await asyncio.sleep(1)
                try:
                    await cb(msg)
                except Exception:
                    logger.exception("outbound dispatch failed after retry")

    def stop(self) -> None:
        self._running = False

    async def drain(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._outbound.join(), timeout=max(0.1, timeout))
            return True
        except asyncio.TimeoutError:
            return False

    async def shutdown(self) -> None:
        self.stop()
        pending = [task for task in self._dispatch_tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def chat_lane(self) -> ChatLane:
        return self._chat_lane

    @property
    def inbound_size(self) -> int:
        return self._inbound.qsize()

    @property
    def outbound_size(self) -> int:
        return self._outbound.qsize()
