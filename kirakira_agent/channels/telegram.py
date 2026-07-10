"""Telegram Bot API channel using stdlib HTTP long polling."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from kirakira_agent.channels.base import AttachmentStore, MessageDeduper
from kirakira_agent.channels.contract import ChannelContext
from kirakira_agent.events import InboundMessage, OutboundMessage
from kirakira_agent.lifecycle import StreamDeltaReady

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
        self._attachments: AttachmentStore | None = None
        self._streams: dict[str, dict[str, Any]] = {}

    async def start(self, ctx: ChannelContext) -> None:
        self._ctx = ctx
        self._attachments = AttachmentStore(ctx.workspace / "uploads" / self.name)
        ctx.bus.subscribe_outbound(self.name, self._on_response)
        ctx.event_bus.on(StreamDeltaReady, self._on_stream_delta)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="telegram_channel_poll")
        ctx.log.info("telegram channel started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._ctx is not None:
            self._ctx.bus.unsubscribe_outbound(self.name, self._on_response)
            self._ctx.event_bus.off(StreamDeltaReady, self._on_stream_delta)
        self._ctx = None
        self._task = None
        self._streams.clear()

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
        if not chat_id or not user_id:
            return
        if not self._allowed(user_id, username):
            logger.warning("[telegram] unauthorized user ignored: id=%s username=%s", user_id, username)
            return
        if self._deduper.seen("%s:%s" % (chat_id, message_id)):
            return
        if text.lower() == "/stop" or text.lower().startswith("/stop@"):
            interrupted = bool(
                ctx.interrupt and ctx.interrupt("%s:%s" % (self.name, chat_id))
            )
            await asyncio.to_thread(
                self._api,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "本轮已中断。" if interrupted else "当前没有正在执行的任务。",
                },
            )
            return
        media = await self._download_media(msg)
        reply = msg.get("reply_to_message") or {}
        if isinstance(reply, dict):
            reply_text = str(reply.get("text") or reply.get("caption") or "").strip()
            if reply_text:
                reply_sender = reply.get("from") or {}
                label = str(reply_sender.get("username") or reply_sender.get("id") or "unknown")
                text = "[回复 @%s]\n%s\n\n%s" % (label, reply_text, text)
        if not text:
            text = "[用户发送了附件]"
        await ctx.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender=user_id,
                chat_id=chat_id,
                content=text,
                media=media,
                metadata={
                    "username": username,
                    "telegram_message_id": message_id,
                    "reply_to_message_id": str(reply.get("message_id") or "")
                    if isinstance(reply, dict)
                    else "",
                },
            )
        )

    async def _on_response(self, msg: OutboundMessage) -> None:
        text = msg.content.strip() or "(empty)"
        stream = self._streams.pop("%s:%s" % (self.name, msg.chat_id), None)
        chunks = self._chunks(text, 4096)
        if stream and stream.get("message_id"):
            try:
                await asyncio.to_thread(
                    self._api,
                    "editMessageText",
                    {
                        "chat_id": msg.chat_id,
                        "message_id": stream["message_id"],
                        "text": chunks[0],
                        "disable_web_page_preview": "true",
                    },
                )
                chunks = chunks[1:]
            except Exception:
                logger.exception("[telegram] final stream edit failed")
        for chunk in chunks:
            await asyncio.to_thread(
                self._api,
                "sendMessage",
                {
                    "chat_id": msg.chat_id,
                    "text": chunk,
                    "disable_web_page_preview": "true",
                },
            )
        for item in msg.media:
            path = Path(item)
            if not path.is_file():
                continue
            await asyncio.to_thread(
                self._send_file,
                msg.chat_id,
                path,
            )

    async def _on_stream_delta(self, event: StreamDeltaReady) -> None:
        if event.channel != self.name or not event.content_delta:
            return
        key = event.session_key
        state = self._streams.setdefault(
            key,
            {"text": "", "message_id": "", "last_edit": 0.0, "iteration": event.iteration},
        )
        if state["iteration"] != event.iteration:
            state["text"] = ""
            state["iteration"] = event.iteration
        state["text"] += event.content_delta
        preview = state["text"][-4096:].strip()
        if not preview:
            return
        if not state["message_id"]:
            response = await asyncio.to_thread(
                self._api,
                "sendMessage",
                {
                    "chat_id": event.chat_id,
                    "text": preview,
                    "disable_web_page_preview": "true",
                },
            )
            state["message_id"] = str((response.get("result") or {}).get("message_id") or "")
            state["last_edit"] = time.monotonic()
            return
        if time.monotonic() - float(state["last_edit"]) < 0.8:
            return
        try:
            await asyncio.to_thread(
                self._api,
                "editMessageText",
                {
                    "chat_id": event.chat_id,
                    "message_id": state["message_id"],
                    "text": preview,
                    "disable_web_page_preview": "true",
                },
            )
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("[telegram] stream edit failed: %s", exc)
        state["last_edit"] = time.monotonic()

    def _allowed(self, user_id: str, username: str) -> bool:
        if not self.allow_from:
            return True
        return user_id.lower() in self.allow_from or username.lower() in self.allow_from

    def _api(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = "https://api.telegram.org/bot%s/%s" % (self.token, method)
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        body = ""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.poll_timeout + 10) as resp:
                    body = resp.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < 2:
                    try:
                        retry_after = float(
                            json.loads(detail).get("parameters", {}).get("retry_after", 1)
                        )
                    except (ValueError, TypeError):
                        retry_after = 1.0
                    time.sleep(min(30.0, max(0.1, retry_after)))
                    continue
                raise RuntimeError(
                    "Telegram API %s failed: HTTP %s %s"
                    % (method, exc.code, detail)
                ) from exc
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
            cut = min(size, len(remaining))
            if cut < len(remaining):
                newline = remaining.rfind("\n", 0, cut + 1)
                if newline >= max(1, size // 2):
                    cut = newline + 1
            chunks.append(remaining[:cut])
            remaining = remaining[cut:]
        return chunks

    async def _download_media(self, msg: dict[str, Any]) -> list[str]:
        attachments = self._attachments
        if attachments is None:
            return []
        file_id = ""
        suffix = ".bin"
        prefix = "file_"
        photos = msg.get("photo") or []
        document = msg.get("document") or {}
        if isinstance(photos, list) and photos:
            photo = photos[-1]
            if isinstance(photo, dict):
                file_id = str(photo.get("file_id") or "")
                suffix = ".jpg"
                prefix = "photo_"
        elif isinstance(document, dict) and document:
            file_id = str(document.get("file_id") or "")
            candidate = Path(str(document.get("file_name") or "")).suffix.lower()
            suffix = candidate if re_safe_suffix(candidate) else ".bin"
            prefix = "document_"
        if not file_id:
            return []
        info = await asyncio.to_thread(self._api, "getFile", {"file_id": file_id})
        file_path = str((info.get("result") or {}).get("file_path") or "")
        if not file_path:
            return []
        url = "https://api.telegram.org/file/bot%s/%s" % (self.token, file_path)

        def download() -> str:
            with urllib.request.urlopen(url, timeout=60) as response:
                declared = int(response.headers.get("content-length") or "0")
                if declared > 20 * 1024 * 1024:
                    raise ValueError("Telegram attachment exceeds 20 MB")
                data = response.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("Telegram attachment exceeds 20 MB")
            return str(attachments.write_bytes(data, prefix=prefix, suffix=suffix))

        return [await asyncio.to_thread(download)]

    def _send_file(self, chat_id: str, path: Path) -> None:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Telegram outbound attachment exceeds 20 MB")
        boundary = "kirakira-telegram-boundary"
        fields = (
            "--%s\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n%s\r\n"
            % (boundary, chat_id)
        ).encode("utf-8")
        file_header = (
            "--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
            % (boundary, path.name.replace('"', ""))
        ).encode("utf-8")
        body = fields + file_header + path.read_bytes() + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        request = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendDocument" % self.token,
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError("Telegram sendDocument failed: %s" % payload)


def re_safe_suffix(value: str) -> bool:
    return bool(value and len(value) <= 12 and value[1:].replace("_", "").isalnum())
