"""Stdlib HTTP web channel.

This is a dependency-light passive web channel. It exposes a tiny chat page and
JSON endpoints, then routes each message through the same MessageBus as every
other channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

from kirakira_agent.channels.contract import ChannelContext
from kirakira_agent.events import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Kirakira Agent</title>
  <style>
    body { margin: 0; font: 15px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f7f8; color: #202124; }
    main { max-width: 860px; margin: 0 auto; min-height: 100vh; display: grid; grid-template-rows: auto 1fr auto; }
    header { padding: 18px 16px; border-bottom: 1px solid #ddd; font-weight: 650; }
    #log { padding: 16px; overflow-y: auto; }
    .msg { margin: 0 0 12px; padding: 10px 12px; border: 1px solid #ddd; background: #fff; border-radius: 6px; white-space: pre-wrap; }
    .user { background: #eef5ff; }
    form { display: flex; gap: 8px; padding: 14px 16px; border-top: 1px solid #ddd; background: #fff; }
    textarea { flex: 1; min-height: 44px; max-height: 160px; resize: vertical; font: inherit; padding: 10px; border: 1px solid #bbb; border-radius: 6px; }
    button { min-width: 72px; border: 1px solid #111; background: #111; color: white; border-radius: 6px; font: inherit; cursor: pointer; }
  </style>
</head>
<body>
<main>
  <header>Kirakira Agent Web Channel</header>
  <section id="log"></section>
  <form id="form">
    <textarea id="text" placeholder="输入消息..."></textarea>
    <button>发送</button>
  </form>
</main>
<script>
const log = document.querySelector("#log");
const text = document.querySelector("#text");
const sessionId = localStorage.kirakiraSessionId || crypto.randomUUID();
localStorage.kirakiraSessionId = sessionId;
function add(cls, value) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = value;
  log.appendChild(div);
  div.scrollIntoView({block: "end"});
}
document.querySelector("#form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const value = text.value.trim();
  if (!value) return;
  text.value = "";
  add("user", value);
  const resp = await fetch("/message", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({session_id: sessionId, text: value})
  });
  const data = await resp.json();
  add("assistant", data.content || data.error || "(empty)");
});
</script>
</body>
</html>
"""


class WebChannel:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        channel_name: str = "web",
        response_timeout: float = 180.0,
    ) -> None:
        self.name = channel_name
        self.host = host
        self.port = int(port)
        self.response_timeout = float(response_timeout)
        self._ctx: ChannelContext | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, list[asyncio.Future[OutboundMessage]]] = {}
        self._lock = threading.Lock()

    async def start(self, ctx: ChannelContext) -> None:
        self._ctx = ctx
        self._loop = asyncio.get_running_loop()
        ctx.bus.subscribe_outbound(self.name, self._on_response)
        handler = self._handler_factory()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="kirakira-web", daemon=True)
        self._thread.start()
        ctx.log.info("web channel listening on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None

    async def _on_response(self, msg: OutboundMessage) -> None:
        futures = []
        with self._lock:
            futures = self._pending.pop(msg.chat_id, [])
        for future in futures:
            if not future.done():
                future.set_result(msg)

    def _handler_factory(self):
        channel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                logger.info("[web] " + fmt, *args)

            def do_GET(self) -> None:
                if self.path == "/" or self.path.startswith("/?"):
                    self._send_bytes(_INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if self.path == "/health":
                    self._send_json({"ok": True, "channel": channel.name})
                    return
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                if self.path != "/message":
                    self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                try:
                    payload = self._read_json()
                    result = channel._handle_message_sync(payload)
                    self._send_json(result)
                except Exception as exc:
                    logger.exception("[web] request failed")
                    self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

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

            def _send_bytes(self, body: bytes, content_type: str) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _handle_message_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._ctx is None or self._loop is None:
            raise RuntimeError("web channel is not started")
        text = str(payload.get("text") or payload.get("content") or "").strip()
        if not text:
            raise ValueError("text/content is required")
        session_id = str(payload.get("session_id") or payload.get("chat_id") or uuid4().hex).strip()
        chat_id = self._chat_id(session_id)
        future = asyncio.run_coroutine_threadsafe(
            self._publish_and_wait(chat_id=chat_id, text=text, payload=payload),
            self._loop,
        )
        msg = future.result(timeout=self.response_timeout + 5)
        return {
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "session_id": "%s:%s" % (self.name, msg.chat_id),
            "content": msg.content,
            "thinking": msg.thinking,
            "media": msg.media,
            "metadata": msg.metadata,
        }

    async def _publish_and_wait(self, *, chat_id: str, text: str, payload: dict[str, Any]) -> OutboundMessage:
        if self._ctx is None:
            raise RuntimeError("web channel is not started")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OutboundMessage] = loop.create_future()
        with self._lock:
            self._pending.setdefault(chat_id, []).append(future)
        try:
            await self._ctx.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender=str(payload.get("sender") or "web"),
                    chat_id=chat_id,
                    content=text,
                    media=[str(item) for item in payload.get("media", []) if str(item).strip()]
                    if isinstance(payload.get("media"), list)
                    else [],
                    metadata={"client_request_id": str(payload.get("request_id") or "")},
                )
            )
            return await asyncio.wait_for(future, timeout=self.response_timeout)
        finally:
            with self._lock:
                futures = self._pending.get(chat_id, [])
                if future in futures:
                    futures.remove(future)
                if not futures:
                    self._pending.pop(chat_id, None)

    def _chat_id(self, session_id: str) -> str:
        prefix = "%s:" % self.name
        if session_id.startswith(prefix):
            return session_id[len(prefix):]
        return session_id
