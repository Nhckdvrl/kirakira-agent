"""Schedule inbound channel work through the conversation pipeline."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus
from plugins.conversation.service import ConversationService

logger = logging.getLogger(__name__)


class ReplyFollower:
    def __init__(
        self, *, bus: MessageBus, pipeline: ConversationService, jobs=None
    ) -> None:
        self.bus = bus
        self.pipeline = pipeline
        self.jobs = jobs
        self._running = False
        self._tasks: set[asyncio.Task[Any]] = set()

    async def run(self) -> None:
        self._running = True
        notices = None
        if self.jobs is not None:
            notices = asyncio.create_task(self._follow_jobs(), name="job-notices")
            self._tasks.add(notices)
            notices.add_done_callback(self._tasks.discard)
        try:
            await self._follow_inputs()
        finally:
            if notices is not None:
                notices.cancel()
                await asyncio.gather(notices, return_exceptions=True)

    async def _follow_inputs(self):
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

    async def _follow_jobs(self):
        async for identity, fact in self.jobs.follow():
            if not self.bus.has_outbound_subscribers(fact["channel"]):
                continue  # Direct callers can consume the result on their next input.
            task = asyncio.create_task(
                self._process_notice(identity, fact["session_key"]),
                name="job-notice:" + identity,
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _process_notice(self, identity, session_key):
        try:
            await self.pipeline.run(None, session_key, job_notice_id=identity)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background result wake failed job=%s", identity)

    async def _process_item(self, item: InboundMessage, key: str) -> None:
        # Pipeline admission is the single queue for channel, direct, control
        # and scheduled turns. A second channel lock would duplicate ownership.
        try:
            await self.pipeline.run(item, key)
        except asyncio.CancelledError:
            logger.info("turn cancelled for session=%s", key)
            raise
        except Exception as exc:
            logger.exception("failed to process inbound")
            metadata = {}
            correlation_id = str(item.metadata.get("client_request_id") or "").strip()
            if correlation_id:
                metadata["client_request_id"] = correlation_id
            await self.bus.publish_outbound(
                OutboundMessage(
                    item.channel, item.chat_id, "出错：%s" % exc, metadata=metadata
                )
            )
        finally:
            await self.bus.complete_inbound(item)

    def stop(self) -> None:
        self._running = False

    def request_interrupt(self, session_key: str) -> bool:
        return self.pipeline.admission.interrupt(session_key)

    def is_busy(self, session_key: str) -> bool:
        return self.pipeline.admission.is_busy(session_key)

    async def shutdown(self) -> None:
        self.stop()
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
