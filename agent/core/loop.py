"""Schedule inbound channel work through the conversation pipeline."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from agent.core.pipeline import PassiveTurnPipeline
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus

logger = logging.getLogger(__name__)
JsonDict = Dict[str, Any]


class AgentLoop:
    def __init__(self, *, bus: MessageBus, pipeline: PassiveTurnPipeline) -> None:
        self.bus = bus
        self.pipeline = pipeline
        self._running = False
        self._active_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._session_locks: Dict[str, asyncio.Lock] = {}

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                item = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            key = item.session_key
            task = asyncio.create_task(
                self._process_item(item, key), name="turn:%s" % key
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _process_item(self, item: InboundMessage, key: str) -> None:
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                task = asyncio.current_task()
                if task is not None:
                    self._active_tasks[key] = task
                try:
                    await self.pipeline.run(item, key)
                except asyncio.CancelledError:
                    logger.info("turn cancelled for session=%s", key)
                    raise
                except Exception as exc:
                    logger.exception("failed to process inbound")
                    metadata = {}
                    correlation_id = str(
                        item.metadata.get("client_request_id") or ""
                    ).strip()
                    if correlation_id:
                        metadata["client_request_id"] = correlation_id
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            item.channel,
                            item.chat_id,
                            "出错：%s" % exc,
                            metadata=metadata,
                        )
                    )
                finally:
                    if self._active_tasks.get(key) is task:
                        self._active_tasks.pop(key, None)
        finally:
            await self.bus.complete_inbound(item)
            current = asyncio.current_task()
            if not lock.locked() and not any(
                task is not current
                and not task.done()
                and task.get_name() == "turn:%s" % key
                for task in self._tasks
            ):
                self._session_locks.pop(key, None)

    def stop(self) -> None:
        self._running = False

    def request_interrupt(self, session_key: str) -> bool:
        admission = getattr(self.pipeline, "admission", None)
        if admission is not None and admission.interrupt(session_key):
            return True
        task = self._active_tasks.get(session_key)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def is_busy(self, session_key: str) -> bool:
        """该 session 是否正在处理一轮被动 turn。供主动链路避让。"""
        admission = getattr(self.pipeline, "admission", None)
        if admission is not None and admission.is_busy(session_key):
            return True
        task = self._active_tasks.get(session_key)
        if task is not None and not task.done():
            return True
        lock = self._session_locks.get(session_key)
        return bool(lock is not None and lock.locked())

    async def shutdown(self) -> None:
        self.stop()
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
