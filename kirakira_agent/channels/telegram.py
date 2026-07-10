"""Telegram Bot API channel using stdlib HTTP long polling."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from kirakira_agent.channels.base import MessageDeduper
from kirakira_agent.channels.contract import ChannelContext
from kirakira_agent.events import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class TelegramChannel:
    def __init__(
        self,
        *,
        token: str,
        allow_from: list[str] | None = None,
        channel_name: str = "telegram",
        poll_timeout: int = 30,
    ) -> None:
        self.name = channel_name
        self.token = token
        self.allow_from = {str(item).lower() for item in (allow_from or []) if str(item).strip()}
        self.poll_timeout = int(poll_timeout)
        self._ctx: ChannelContext | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._offset = 0
        self._deduper = MessageDeduper()

    async def start(self, ctx: ChannelContext) -> None:
        self._ctx = ctx
        ctx.bus.subscribe_outbound(self.name, self._on_response)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="telegram_channel_poll")
        ctx.log.info("telegram channel started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = await asyncio.to_thread(self._api, "getUpdates", {
                    "offset": self._offset,
                    "timeout": self.poll_timeout,
                    "allowed_updates": json.dumps(["message"]),
                })
                for update in updates.get("result", []):
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[telegram] polling failed: %s", exc)
                await asyncio.sleep(2)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        msg = update.get("message") or {}
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        chat_id = str(chat.get("id") or "").strip()
        user_id = str(sender.get("id") or "").strip()
        username = str(sender.get("username") or "").strip()
        text = str(msg.get("text") or msg.get("caption") or "").strip()
        message_id = str(msg.get("message_id") or "").strip()
        if not chat_id or not user_id or not text:
            return
        if not self._allowed(user_id, username):
            logger.warning("[telegram] unauthorized user ignored: id=%s username=%s", user_id, username)
            return
        if self._deduper.seen("%s:%s" % (chat_id, message_id)):
            return
        await ctx.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender=user_id,
                chat_id=chat_id,
                content=text,
                metadata={
                    "username": username,
                    "telegram_message_id": message_id,
                },
            )
        )

    async def _on_response(self, msg: OutboundMessage) -> None:
        text = msg.content.strip() or "(empty)"
        for chunk in self._chunks(text, 4096):
            await asyncio.to_thread(self._api, "sendMessage", {
                "chat_id": msg.chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            })

    def _allowed(self, user_id: str, username: str) -> bool:
        if not self.allow_from:
            return True
        return user_id.lower() in self.allow_from or username.lower() in self.allow_from

    def _api(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = "https://api.telegram.org/bot%s/%s" % (self.token, method)
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.poll_timeout + 10) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("Telegram API %s failed: HTTP %s %s" % (method, exc.code, detail)) from exc
        payload = json.loads(body)
        if not payload.get("ok"):
            raise RuntimeError("Telegram API %s failed: %s" % (method, payload))
        return payload

    def _chunks(self, text: str, size: int) -> list[str]:
        if len(text) <= size:
            return [text]
        chunks = []
        remaining = text
        while remaining:
            chunks.append(remaining[:size])
            remaining = remaining[size:]
        return chunks
