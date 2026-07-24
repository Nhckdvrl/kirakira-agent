"""Stdlib HTTP web channel.

This is a dependency-light passive web channel. It exposes a tiny chat page and
JSON endpoints, then routes each message through the same MessageBus as every
other channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import urllib.parse
from uuid import uuid4

from kirakira_agent.channels.contract import ChannelContext
from kirakira_agent.events import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class _DaemonThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


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
async function pollEvents() {
  while (true) {
    try {
      const response = await fetch("/events?session_id=" + encodeURIComponent(sessionId));
      if (response.ok) {
        const data = await response.json();
        if (data.content) add("agent", data.content);
      }
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 500));
  }
}
pollEvents();
</script>
</body>
</html>
"""


_MEMORY_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Kirakira Memory2</title>
  <style>
    body { margin: 0; font: 14px/1.5 system-ui, sans-serif; color: #202124; background: #f6f7f9; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; }
    h1 { margin: 0; font-size: 22px; }
    a { color: #275efe; }
    .panel { background: white; border: 1px solid #ddd; border-radius: 8px; padding: 14px; margin-bottom: 14px; }
    .filters { display: grid; grid-template-columns: 2fr repeat(3, 1fr) auto; gap: 8px; }
    input, select, textarea, button { font: inherit; padding: 8px; border: 1px solid #bbb; border-radius: 5px; }
    button { cursor: pointer; background: #fff; }
    button.primary { background: #111; color: #fff; border-color: #111; }
    button.danger { color: #a00; border-color: #c88; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e4e4e4; padding: 8px; text-align: left; vertical-align: top; }
    th { white-space: nowrap; }
    td.summary { max-width: 560px; }
    .muted { color: #777; }
    .toolbar { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
    #detail[hidden] { display: none; }
    #detail textarea { width: 100%; min-height: 100px; box-sizing: border-box; }
    pre { white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow: auto; }
  </style>
</head>
<body><main>
  <header><h1>Kirakira Memory2 Dashboard</h1><a href="/">返回聊天</a></header>
  <section class="panel filters">
    <input id="q" placeholder="搜索 ID、正文、source_ref" />
    <select id="type"><option value="">全部类型</option><option>procedure</option><option>preference</option><option>event</option><option>profile</option></select>
    <select id="status"><option value="">全部状态</option><option>active</option><option>superseded</option></select>
    <select id="embedding"><option value="">全部向量状态</option><option value="true">有 embedding</option><option value="false">无 embedding</option></select>
    <button class="primary" onclick="page=1;load()">查询</button>
  </section>
  <section class="panel">
    <table><thead><tr><th></th><th>ID</th><th>类型/状态</th><th>摘要</th><th>来源</th><th>强化</th><th></th></tr></thead><tbody id="rows"></tbody></table>
    <div class="toolbar">
      <button onclick="page=Math.max(1,page-1);load()">上一页</button>
      <span id="pageInfo"></span>
      <button onclick="page++;load()">下一页</button>
      <button class="danger" onclick="batchHardDelete()">确认硬删除选中项</button>
    </div>
  </section>
  <section id="detail" class="panel" hidden>
    <h2 id="detailTitle"></h2>
    <label>正文</label><textarea id="summary"></textarea>
    <div class="toolbar">
      <select id="editType"><option>procedure</option><option>preference</option><option>event</option><option>profile</option></select>
      <button class="primary" onclick="saveEdit()">保存编辑</button>
      <button onclick="logicalDelete()">逻辑删除</button>
      <button onclick="loadSimilar()">相似记忆</button>
      <button class="danger" onclick="hardDelete()">确认硬删除</button>
    </div>
    <pre id="detailJson"></pre><pre id="similar"></pre>
  </section>
  <section class="panel"><button onclick="health()">刷新健康信息</button><pre id="health"></pre></section>
</main>
<script>
let page=1, pageSize=50, selected=null;
const qEl=document.getElementById('q'), typeEl=document.getElementById('type'), statusEl=document.getElementById('status'), embeddingEl=document.getElementById('embedding');
const rowsEl=document.getElementById('rows'), pageInfoEl=document.getElementById('pageInfo'), detailEl=document.getElementById('detail');
const detailTitleEl=document.getElementById('detailTitle'), summaryEl=document.getElementById('summary'), editTypeEl=document.getElementById('editType');
const detailJsonEl=document.getElementById('detailJson'), similarEl=document.getElementById('similar'), healthEl=document.getElementById('health');
const esc = value => String(value ?? "").replace(/[&<>\"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function load(){
  const p=new URLSearchParams({page,page_size:pageSize,q:qEl.value,memory_type:typeEl.value,status:statusEl.value,has_embedding:embeddingEl.value});
  const data=await (await fetch('/api/memories?'+p)).json();
  rowsEl.innerHTML=data.memories.map(item=>`<tr><td><input type="checkbox" value="${esc(item.id)}"></td><td><a href="#" onclick="showDetail('${esc(item.id)}');return false">${esc(item.id)}</a></td><td>${esc(item.memory_type)}<br><span class="muted">${esc(item.status)} / ${item.has_embedding?'vector':'no vector'}</span></td><td class="summary">${esc(item.summary)}</td><td>${esc(item.source_ref||'')}</td><td>${esc(item.reinforcement)}</td><td><button onclick="showDetail('${esc(item.id)}')">详情</button></td></tr>`).join('');
  const pages=Math.max(1,Math.ceil(data.total/data.page_size)); if(page>pages){page=pages;return load();}
  pageInfoEl.textContent=`第 ${page}/${pages} 页，共 ${data.total} 条`;
}
async function showDetail(id){
  const response=await fetch('/api/memory?id='+encodeURIComponent(id)); const data=await response.json();
  if(!response.ok){alert(data.error||'not found');return;} selected=data.memory; detailEl.hidden=false;
  detailTitleEl.textContent=`记忆 ${selected.id}`; summaryEl.value=selected.summary; editTypeEl.value=selected.memory_type;
  detailJsonEl.textContent=JSON.stringify(selected,null,2); similarEl.textContent=''; detailEl.scrollIntoView({behavior:'smooth'});
}
async function saveEdit(){
  const response=await fetch('/api/memory',{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({id:selected.id,content:summaryEl.value,memory_type:editTypeEl.value})});
  const data=await response.json(); if(!response.ok){alert(data.error);return;} await load(); await showDetail(selected.id);
}
async function logicalDelete(){if(!selected||!confirm('将该记忆标记为 superseded？'))return;await fetch('/api/memory?id='+encodeURIComponent(selected.id),{method:'DELETE'});detailEl.hidden=true;load();}
async function hardDelete(){if(!selected||!confirm('物理删除不可由普通 recall 恢复，确定？'))return;await fetch('/api/memory?id='+encodeURIComponent(selected.id)+'&hard=true&confirm=HARD_DELETE',{method:'DELETE'});detailEl.hidden=true;load();}
async function batchHardDelete(){const ids=[...document.querySelectorAll('tbody input:checked')].map(x=>x.value);if(!ids.length||!confirm(`物理删除 ${ids.length} 条记忆？`))return;const r=await fetch('/api/memories/delete',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({ids,confirm:'HARD_DELETE'})});const d=await r.json();if(!r.ok)alert(d.error);load();}
async function loadSimilar(){const r=await fetch('/api/memory/similar?id='+encodeURIComponent(selected.id));const d=await r.json();similarEl.textContent=JSON.stringify(d,null,2);}
async function health(){healthEl.textContent=JSON.stringify(await (await fetch('/api/memory/health')).json(),null,2);}
load();health();
</script></body></html>
"""


class WebChannel:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 6322,
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
        self._pending: dict[str, asyncio.Future[OutboundMessage]] = {}
        self._event_queues: dict[str, asyncio.Queue[OutboundMessage]] = {}
        self._lock = threading.Lock()

    async def start(self, ctx: ChannelContext) -> None:
        self._ctx = ctx
        self._loop = asyncio.get_running_loop()
        ctx.bus.subscribe_outbound(self.name, self._on_response)
        handler = self._handler_factory()
        self._server = _DaemonThreadingHTTPServer((self.host, self.port), handler)
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
        if self._ctx is not None:
            self._ctx.bus.unsubscribe_outbound(self.name, self._on_response)
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.cancel()
        self._ctx = None
        self._loop = None

    async def _on_response(self, msg: OutboundMessage) -> None:
        request_id = str(msg.metadata.get("client_request_id") or "")
        future = None
        with self._lock:
            if request_id:
                future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(msg)
            return
        if not request_id:
            queue = self._event_queues.setdefault(msg.chat_id, asyncio.Queue())
            await queue.put(msg)

    def _handler_factory(self):
        channel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                logger.info("[web] " + fmt, *args)

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/":
                    self._send_bytes(_INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if parsed.path == "/memory":
                    self._send_bytes(_MEMORY_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if parsed.path == "/favicon.ico":
                    self._send_bytes(b"", "image/x-icon")
                    return
                if parsed.path == "/health":
                    self._send_json({"ok": True, "channel": channel.name})
                    return
                if parsed.path == "/api/sessions":
                    self._send_json(
                        {"sessions": channel._ctx.session_manager.list_sessions()}
                        if channel._ctx
                        else {"sessions": []}
                    )
                    return
                if parsed.path == "/api/memories":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(channel._memory_records(query))
                    return
                if parsed.path == "/api/memory":
                    query = urllib.parse.parse_qs(parsed.query)
                    item = channel._memory_detail(str((query.get("id") or [""])[0]))
                    self._send_json(
                        {"memory": item},
                        status=HTTPStatus.OK if item is not None else HTTPStatus.NOT_FOUND,
                    )
                    return
                if parsed.path == "/api/memory/similar":
                    query = urllib.parse.parse_qs(parsed.query)
                    try:
                        items = channel._memory_similar(
                            str((query.get("id") or [""])[0]),
                            int((query.get("limit") or ["8"])[0]),
                        )
                        self._send_json({"items": items})
                    except Exception as exc:
                        self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                if parsed.path == "/api/memory/health":
                    self._send_json(channel._memory_health())
                    return
                if parsed.path == "/events":
                    try:
                        query = urllib.parse.parse_qs(parsed.query)
                        session_id = str((query.get("session_id") or [""])[0])
                        if not session_id:
                            raise ValueError("session_id is required")
                        message = channel._next_event_sync(channel._chat_id(session_id))
                        self._send_json(
                            {
                                "content": message.content,
                                "media": message.media,
                                "metadata": message.metadata,
                            }
                        )
                    except TimeoutError:
                        self._send_json({"content": ""})
                    except Exception as exc:
                        self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                if self.path == "/api/memories/delete":
                    try:
                        payload = self._read_json()
                        deleted = channel._hard_delete_memories(
                            [str(item) for item in payload.get("ids", [])],
                            confirm=str(payload.get("confirm") or ""),
                        )
                        self._send_json({"ok": True, "deleted": deleted})
                    except Exception as exc:
                        self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                if self.path == "/interrupt":
                    try:
                        payload = self._read_json()
                        session_id = str(payload.get("session_id") or payload.get("chat_id") or "")
                        if not session_id or channel._ctx is None or channel._ctx.interrupt is None:
                            raise ValueError("session_id is required and interrupt must be enabled")
                        stopped = channel._ctx.interrupt(
                            "%s:%s" % (channel.name, channel._chat_id(session_id))
                        )
                        self._send_json({"ok": True, "interrupted": stopped})
                    except Exception as exc:
                        self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
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

            def do_PATCH(self) -> None:
                if self.path != "/api/memory":
                    self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                try:
                    payload = self._read_json()
                    updated = channel._update_memory(payload)
                    self._send_json({"ok": updated}, status=HTTPStatus.OK if updated else HTTPStatus.NOT_FOUND)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

            def do_DELETE(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/api/session":
                    key = str((query.get("key") or [""])[0])
                    deleted = bool(channel._ctx and channel._ctx.session_manager.delete_session(key))
                    self._send_json({"ok": deleted}, status=HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
                    return
                if parsed.path == "/api/memory":
                    memory_id = str((query.get("id") or [""])[0])
                    hard = str((query.get("hard") or [""])[0]).lower() in {"1", "true"}
                    if hard:
                        try:
                            deleted = bool(
                                channel._hard_delete_memories(
                                    [memory_id],
                                    confirm=str((query.get("confirm") or [""])[0]),
                                )
                            )
                        except Exception as exc:
                            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                            return
                    else:
                        deleted = channel._forget_memory(memory_id)
                    self._send_json({"ok": deleted}, status=HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
                    return
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("content-length") or "0")
                if length < 0 or length > 1024 * 1024:
                    raise ValueError("request body exceeds 1 MB")
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
        if not session_id or len(session_id) > 200:
            raise ValueError("session_id must contain 1-200 characters")
        chat_id = self._chat_id(session_id)
        if text.lower() == "/stop":
            interrupted = bool(
                self._ctx.interrupt
                and self._ctx.interrupt("%s:%s" % (self.name, chat_id))
            )
            return {
                "channel": self.name,
                "chat_id": chat_id,
                "session_id": "%s:%s" % (self.name, chat_id),
                "content": "本轮已中断。" if interrupted else "当前没有正在执行的任务。",
                "thinking": "",
                "media": [],
                "metadata": {"interrupted": interrupted},
            }
        request_id = str(payload.get("request_id") or uuid4().hex).strip()
        payload = {**payload, "request_id": request_id}
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
        request_id = str(payload["request_id"])
        with self._lock:
            if request_id in self._pending:
                raise ValueError("duplicate request_id: %s" % request_id)
            self._pending[request_id] = future
        try:
            media = self._validated_media(payload.get("media"))
            await self._ctx.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender=str(payload.get("sender") or "web"),
                    chat_id=chat_id,
                    content=text,
                    media=media,
                    metadata={"client_request_id": request_id},
                )
            )
            return await asyncio.wait_for(future, timeout=self.response_timeout)
        finally:
            with self._lock:
                if self._pending.get(request_id) is future:
                    self._pending.pop(request_id, None)

    def _chat_id(self, session_id: str) -> str:
        prefix = "%s:" % self.name
        if session_id.startswith(prefix):
            return session_id[len(prefix):]
        return session_id

    def _next_event_sync(self, chat_id: str) -> OutboundMessage:
        if self._loop is None:
            raise RuntimeError("web channel is not started")

        async def wait() -> OutboundMessage:
            queue = self._event_queues.setdefault(chat_id, asyncio.Queue())
            return await asyncio.wait_for(queue.get(), timeout=25.0)

        future = asyncio.run_coroutine_threadsafe(wait(), self._loop)
        try:
            return future.result(timeout=30.0)
        except Exception:
            future.cancel()
            raise TimeoutError

    def _validated_media(self, value: object) -> list[str]:
        if self._ctx is None or not isinstance(value, list):
            return []
        root = self._ctx.workspace.resolve()
        result = []
        for item in value[:8]:
            path = Path(str(item)).expanduser()
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError("media path escapes workspace") from exc
            if not path.is_file():
                raise ValueError("media file does not exist: %s" % item)
            result.append(str(path))
        return result

    def _memory_records(self, query: dict[str, list[str]] | None = None) -> dict[str, object]:
        if self._ctx is None:
            return {"memories": [], "total": 0, "page": 1, "page_size": 50}
        memory = getattr(self._ctx, "memory", None)
        if memory is None:
            return {"memories": [], "total": 0, "page": 1, "page_size": 50}
        params = query or {}
        page = max(1, int((params.get("page") or ["1"])[0]))
        page_size = max(1, min(200, int((params.get("page_size") or ["50"])[0])))
        if memory.engine == "coremem" and memory.store2 is not None:
            embedding_raw = str((params.get("has_embedding") or [""])[0]).lower()
            has_embedding = None
            if embedding_raw in {"1", "true"}:
                has_embedding = True
            elif embedding_raw in {"0", "false"}:
                has_embedding = False
            items, total = memory.store2.list_items_for_dashboard(
                q=str((params.get("q") or [""])[0]),
                memory_type=str((params.get("memory_type") or [""])[0]),
                status=str((params.get("status") or [""])[0]),
                source_ref=str((params.get("source_ref") or [""])[0]),
                scope_channel=str((params.get("scope_channel") or [""])[0]),
                scope_chat_id=str((params.get("scope_chat_id") or [""])[0]),
                has_embedding=has_embedding,
                page=page,
                page_size=page_size,
                sort_by=str((params.get("sort_by") or ["created_at"])[0]),
                sort_order=str((params.get("sort_order") or ["desc"])[0]),
            )
            return {"memories": items, "total": total, "page": page, "page_size": page_size}
        items = memory.list_records(include_forgotten=True)
        return {
            "memories": items[(page - 1) * page_size : page * page_size],
            "total": len(items),
            "page": page,
            "page_size": page_size,
        }

    def _memory_detail(self, memory_id: str) -> dict[str, object] | None:
        if self._ctx is None:
            return None
        memory = getattr(self._ctx, "memory", None)
        if memory is None:
            return None
        if memory.engine == "coremem" and memory.store2 is not None:
            return memory.store2.get_item_for_dashboard(memory_id)
        return next(
            (item for item in memory.list_records(include_forgotten=True) if item["id"] == memory_id),
            None,
        )

    def _memory_similar(self, memory_id: str, limit: int) -> list[dict[str, object]]:
        if self._ctx is None:
            return []
        memory = getattr(self._ctx, "memory", None)
        if memory is None or memory.engine != "coremem" or memory.store2 is None:
            return []
        return memory.store2.find_similar_items_for_dashboard(
            memory_id, top_k=max(1, min(50, limit))
        )

    def _memory_health(self) -> dict[str, object]:
        if self._ctx is None:
            return {"ok": False, "error": "channel not started"}
        from kirakira_agent.memory_admin import doctor

        return doctor(self._ctx.workspace)

    def _update_memory(self, payload: dict[str, Any]) -> bool:
        if self._ctx is None:
            return False
        memory = getattr(self._ctx, "memory", None)
        if memory is None:
            return False
        return memory.update_record(
            str(payload.get("id") or ""),
            content=payload.get("content"),
            memory_type=payload.get("memory_type"),
        )

    def _forget_memory(self, memory_id: str) -> bool:
        if self._ctx is None:
            return False
        memory = getattr(self._ctx, "memory", None)
        return bool(memory and memory.forget([memory_id]))

    def _hard_delete_memories(self, ids: list[str], *, confirm: str) -> int:
        if confirm != "HARD_DELETE":
            raise ValueError("hard delete requires confirm=HARD_DELETE")
        if self._ctx is None:
            return 0
        memory = getattr(self._ctx, "memory", None)
        if memory is None or memory.engine != "coremem" or memory.store2 is None:
            raise RuntimeError("hard delete 只允许在 Memory2 Dashboard 执行")
        deleted = memory.store2.delete_items_batch([item for item in ids if item])
        memory._load()
        return deleted
