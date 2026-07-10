"""QQ channel via OneBot/NapCat HTTP webhook and HTTP API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kirakira_agent.channels.base import MessageDeduper
from kirakira_agent.channels.contract import ChannelContext
from kirakira_agent.events import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)
_GROUP_PREFIX = "gqq:"
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)[^\]]*\]")


class QQChannel:
    def __init__(
        self,
        *,
        bot_uin: str = "",
        api_base_url: str = "http://127.0.0.1:3000",
        webhook_host: str = "127.0.0.1",
        webhook_port: int = 8766,
        access_token: str = "",
        allow_from: list[str] | None = None,
        group_allow: list[str] | None = None,
        require_at: bool = True,
        channel_name: str = "qq",
    ) -> None:
        self.name = channel_name
        self.bot_uin = str(bot_uin or "")
        self.api_base_url = api_base_url.rstrip("/")
        self.webhook_host = webhook_host
        self.webhook_port = int(webhook_port)
        self.access_token = access_token
        self.allow_from = {str(item) for item in (allow_from or []) if str(item).strip()}
        self.group_allow = {str(item) for item in (group_allow or []) if str(item).strip()}
        self.require_at = bool(require_at)
        self._ctx: ChannelContext | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._deduper = MessageDeduper()

    async def start(self, ctx: ChannelContext) -> None:
        self._ctx = ctx
        self._loop = asyncio.get_running_loop()
        ctx.bus.subscribe_outbound(self.name, self._on_response)
        handler = self._handler_factory()
        self._server = ThreadingHTTPServer((self.webhook_host, self.webhook_port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="kirakira-qq", daemon=True)
        self._thread.start()
        ctx.log.info("qq channel webhook listening on http://%s:%s/qq/webhook", self.webhook_host, self.webhook_port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None

    async def _on_response(self, msg: OutboundMessage) -> None:
        if msg.chat_id.startswith(_GROUP_PREFIX):
            await asyncio.to_thread(
                self._api,
                "send_group_msg",
                {
                    "group_id": msg.chat_id[len(_GROUP_PREFIX):],
                    "message": msg.content,
                },
            )
            return
        await asyncio.to_thread(
            self._api,
            "send_private_msg",
            {
                "user_id": msg.chat_id,
                "message": msg.content,
            },
        )

    async def _handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        ctx = self._ctx
        if ctx is None:
            return {"ok": False, "error": "channel not started"}
        post_type = str(payload.get("post_type") or "")
        message_type = str(payload.get("message_type") or "")
        if post_type and post_type != "message":
            return {"ok": True, "ignored": "post_type"}
        raw_message = str(payload.get("raw_message") or payload.get("message") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        message_id = str(payload.get("message_id") or "").strip()
        if not raw_message or not user_id:
            return {"ok": True, "ignored": "empty"}
        if self._deduper.seen("%s:%s:%s" % (message_type, user_id, message_id)):
            return {"ok": True, "ignored": "duplicate"}

        if message_type == "group":
            group_id = str(payload.get("group_id") or "").strip()
            if not group_id:
                return {"ok": True, "ignored": "missing_group"}
            if self.group_allow and group_id not in self.group_allow:
                return {"ok": True, "ignored": "group_not_allowed"}
            if self.allow_from and user_id not in self.allow_from:
                return {"ok": True, "ignored": "user_not_allowed"}
            if self.require_at and self.bot_uin and not self._is_at_bot(raw_message):
                return {"ok": True, "ignored": "not_at_bot"}
            chat_id = _GROUP_PREFIX + group_id
            content = self._strip_at(raw_message)
        else:
            if self.allow_from and user_id not in self.allow_from:
                return {"ok": True, "ignored": "user_not_allowed"}
            chat_id = user_id
            content = raw_message

        if not content:
            return {"ok": True, "ignored": "empty_after_filter"}
        await ctx.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender=user_id,
                chat_id=chat_id,
                content=content,
                metadata={
                    "qq_message_id": message_id,
                    "message_type": message_type,
                    "group_id": str(payload.get("group_id") or ""),
                },
            )
        )
        return {"ok": True, "chat_id": chat_id}

    def _handler_factory(self):
        channel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                logger.info("[qq] " + fmt, *args)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send_json({"ok": True, "channel": channel.name})
                    return
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                if self.path not in ("/", "/qq/webhook", "/onebot"):
                    self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                try:
                    payload = self._read_json()
                    if channel._loop is None:
                        raise RuntimeError("qq channel loop missing")
                    future = asyncio.run_coroutine_threadsafe(channel._handle_event(payload), channel._loop)
                    self._send_json(future.result(timeout=10))
                except Exception as exc:
                    logger.exception("[qq] webhook failed")
                    self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("content-length") or "0")
                raw = self.rfile.read(length).decode("utf-8")
                data = json.loads(raw or "{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON body must be an object")
                return data

            def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(int(status))
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _api(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = "%s/%s" % (self.api_base_url, action)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = "Bearer %s" % self.access_token
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("OneBot API %s failed: HTTP %s %s" % (action, exc.code, detail)) from exc
        return json.loads(body or "{}")

    def _is_at_bot(self, raw: str) -> bool:
        return any(qq == self.bot_uin for qq in _CQ_AT_RE.findall(raw))

    def _strip_at(self, raw: str) -> str:
        return _CQ_AT_RE.sub("", raw).strip()

