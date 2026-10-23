"""Render transient reply previews without delaying model callbacks or committing messages."""

import asyncio
import hashlib
import logging
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager

from agent.plugin_composition.tasks import child_task_context
from core.common.diagnostic_log import log_timing
from plugins.reply.contract import REPLY_STATUS

logger = logging.getLogger(__name__)


def draft_text(text: str) -> str:
    """Telegram offsets and limits count UTF-16 code units."""
    return text.encode("utf-16-le")[:8192].decode("utf-16-le", errors="ignore")


class DraftDeliveries:
    """Fence the actual sender as well as direct channel delivery, per bot/chat."""

    def __init__(self):
        self.readers = {}

    def register(self, identity, reader):
        self.readers.setdefault(identity, set()).add(reader)

        def remove():
            readers = self.readers.get(identity)
            if readers is not None:
                readers.discard(reader)
                if not readers:
                    del self.readers[identity]

        return remove

    @asynccontextmanager
    async def delivering(self, identity, chat_id, message_id):
        async with AsyncExitStack() as stack:
            for reader in tuple(self.readers.get(identity, ())):
                await stack.enter_async_context(reader.delivering(chat_id, message_id))
            yield


class ReplyDrafts:
    """One conflating, cancellable reader per admitted private chat.

    Only the public ReplyStatus read contract is borrowed. Drafts are ephemeral;
    the adapter's ordinary final delivery retains all durable receipt semantics.
    """

    def __init__(self, context, bot, *, interval=1.0, timeout=2.0, min_chars=64, supervised=False):
        self.context, self.bot = context, bot
        self.interval, self.timeout = interval, timeout
        self.min_chars = min_chars
        self.tasks = {}
        self.locks = {}
        self.chats = set()
        self.finished = OrderedDict()
        self.closed = False
        self.supervised = supervised
        self.group = None

    async def run(self):
        """A Fiber-owned supervisor drains all subscriptions before owner release."""
        try:
            async with asyncio.TaskGroup() as group:
                self.group = group
                for chat_id in self.chats:
                    self.watch(chat_id)
                try:
                    await asyncio.Future()
                finally:
                    self.closed = True
        finally:
            self.closed = True
            self.group = None

    def watch(self, chat_id: int) -> None:
        if chat_id <= 0 or self.closed or self.context.open_scope is None:
            return
        self.chats.add(chat_id)
        if chat_id in self.tasks:
            return
        if self.supervised and self.group is None:
            return
        spawn = asyncio.create_task if self.group is None else self.group.create_task
        task = spawn(self._follow(chat_id), name="telegram-drafts", context=child_task_context())
        self.tasks[chat_id] = task

        def settled(completed):
            if self.tasks.get(chat_id) is completed:
                del self.tasks[chat_id]
            if completed.cancelled() and not self.closed:
                # Admission can reopen before cancellation has finished draining.
                self.watch(chat_id)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    logger.warning("Telegram 草稿停止：%s", type(error).__name__)

        task.add_done_callback(settled)

    def pause(self) -> None:
        self.closed = True
        for task in tuple(self.tasks.values()):
            task.cancel()

    def resume(self) -> None:
        self.closed = False
        for chat_id in self.chats:
            self.watch(chat_id)

    async def close(self) -> None:
        self.pause()
        await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)

    @asynccontextmanager
    async def delivering(self, chat_id: int, message_id: str | None):
        """Drain any earlier preview before the normal final message clears it."""
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            if message_id:
                self.finished[message_id] = None
                while len(self.finished) > 256:
                    self.finished.popitem(last=False)
            yield

    async def _follow(self, chat_id: int) -> None:
        from telegram.error import RetryAfter, TelegramError

        session = "telegram:" + str(chat_id)
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        next_at = 0.0
        previous = None
        generation = None
        async with self.context.open_scope() as request:
            with request.borrow(REPLY_STATUS) as status:
                if status is None:
                    return
                changes = status.follow(session)
                try:
                    async for _ in changes:
                        visible = [item for item in status.snapshot(session)
                                   if item.active and item.source == "conversation"
                                   and item.preview is not None and item.preview.text
                                   and item.preview.message_id not in self.finished]
                        if not visible:
                            continue
                        identity = visible[-1].preview.message_id
                        if identity != generation:
                            generation = identity
                            # Tiny replies usually finish in this batching window.
                            # Their final send must not wait for a redundant draft.
                            next_at = max(next_at, asyncio.get_running_loop().time() + min(0.15, self.interval))
                        delay = next_at - asyncio.get_running_loop().time()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        async with lock:
                            # Read again after waiting: tokens coalesce, and a
                            # revoked or completed generation must not reappear.
                            candidates = [item for item in status.snapshot(session)
                                          if item.active and item.source == "conversation"
                                          and item.preview is not None and item.preview.text
                                          and item.preview.message_id not in self.finished]
                            if self.closed or not candidates:
                                continue
                            item = candidates[-1]
                            preview = item.preview
                            text = draft_text(preview.text)
                            current = (preview.message_id, text)
                            # A short answer should use its single durable send;
                            # an almost simultaneous draft adds another network
                            # request that final delivery must drain.
                            if len(text) < self.min_chars or not text or current == previous:
                                continue
                            identity = int.from_bytes(hashlib.sha256(item.handle.encode()).digest()[:8], "big") & ((1 << 63) - 1)
                            log_timing("telegram.draft.begin", session_id=session, request_id=preview.message_id)
                            try:
                                async with asyncio.timeout(self.timeout):
                                    await self.bot().send_message_draft(
                                        chat_id=chat_id, draft_id=identity or 1, text=text,
                                        connect_timeout=self.timeout, read_timeout=self.timeout,
                                        write_timeout=self.timeout, pool_timeout=self.timeout,
                                    )
                            except RetryAfter as error:
                                retry = error.retry_after
                                seconds = retry.total_seconds() if hasattr(retry, "total_seconds") else float(retry)
                                next_at = asyncio.get_running_loop().time() + max(self.interval, seconds)
                                continue
                            except asyncio.CancelledError:
                                raise
                            except (TelegramError, OSError, TimeoutError) as error:
                                # Presentation failure does not retry or fail a
                                # model call, nor block normal final delivery.
                                logger.warning("Telegram 草稿不可用：%s", type(error).__name__)
                                return
                            finally:
                                log_timing("telegram.draft.end", session_id=session, request_id=preview.message_id)
                            previous = current
                            next_at = asyncio.get_running_loop().time() + self.interval
                finally:
                    await changes.aclose()
