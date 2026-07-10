"""Channel integration tests."""

import asyncio
import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kirakira_agent.bus import MessageBus
from kirakira_agent.channels.contract import ChannelContext
from kirakira_agent.channels.qq import QQChannel
from kirakira_agent.channels.telegram import TelegramChannel
from kirakira_agent.channels.web import WebChannel
from kirakira_agent.context_builder import ContextBuilder
from kirakira_agent.event_bus import EventBus
from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.runtime import AgentLoop, DefaultReasoner, PassiveTurnPipeline, RuntimeConfig
from kirakira_agent.schema import ModelResponse
from kirakira_agent.session import SessionManager
from kirakira_agent.tools import build_default_registry


class FakeModel:
    def __init__(self, text):
        self.text = text

    def complete(self, messages, tools, system, model, max_tokens):
        return ModelResponse(text=self.text)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def build_runtime(workdir, text):
    bus = MessageBus()
    event_bus = EventBus()
    sessions = SessionManager(workdir)
    memory = MemoryRuntime(workdir, session_manager=sessions)
    tools = build_default_registry(workdir, memory=memory, session_manager=sessions)
    context = ContextBuilder(workdir, memory)
    config = RuntimeConfig(model="fake", max_iterations=3, max_tokens=1000, history_window=20)
    reasoner = DefaultReasoner(
        model_client=FakeModel(text),
        tools=tools,
        config=config,
        context=context,
        event_bus=event_bus,
    )
    pipeline = PassiveTurnPipeline(
        bus=bus,
        event_bus=event_bus,
        session_manager=sessions,
        memory=memory,
        tools=tools,
        reasoner=reasoner,
        config=config,
    )
    return bus, event_bus, sessions, AgentLoop(bus=bus, pipeline=pipeline)


async def start_core(bus, loop):
    return [
        asyncio.create_task(loop.run()),
        asyncio.create_task(bus.dispatch_outbound()),
    ]


async def stop_core(bus, loop, tasks):
    loop.stop()
    bus.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class FakeOneBotServer:
    def __init__(self, body=b'{"status":"ok","retcode":0}'):
        self.port = free_port()
        self.received = []
        self.body = body
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                outer.received.append((self.path, payload))
                body = outer.body
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class ChannelTests(unittest.TestCase):
    def test_web_channel_posts_message_and_returns_agent_reply(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                bus, event_bus, sessions, loop = build_runtime(workdir, "web ok")
                port = free_port()
                channel = WebChannel(host="127.0.0.1", port=port)
                ctx = ChannelContext(bus, sessions, event_bus, workdir, __import__("logging").getLogger("test.web"))
                tasks = await start_core(bus, loop)
                await channel.start(ctx)
                try:
                    data = json.dumps({"session_id": "test-web", "text": "hello"}).encode("utf-8")
                    req = urllib.request.Request(
                        "http://127.0.0.1:%d/message" % port,
                        data=data,
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    body = await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=10).read())
                    payload = json.loads(body.decode("utf-8"))
                    self.assertEqual(payload["content"], "web ok")
                    self.assertEqual(sessions.get_or_create("web:test-web").messages[-1]["content"], "web ok")
                finally:
                    await channel.stop()
                    await stop_core(bus, loop, tasks)

        asyncio.run(scenario())

    def test_qq_channel_webhook_routes_group_message_and_sends_reply(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                onebot = FakeOneBotServer()
                onebot.start()
                bus, event_bus, sessions, loop = build_runtime(workdir, "qq ok")
                port = free_port()
                channel = QQChannel(
                    bot_uin="12345",
                    api_base_url="http://127.0.0.1:%d" % onebot.port,
                    webhook_host="127.0.0.1",
                    webhook_port=port,
                    group_allow=["777"],
                    require_at=True,
                )
                ctx = ChannelContext(bus, sessions, event_bus, workdir, __import__("logging").getLogger("test.qq"))
                tasks = await start_core(bus, loop)
                await channel.start(ctx)
                try:
                    event = {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 777,
                        "user_id": 888,
                        "message_id": 1,
                        "raw_message": "[CQ:at,qq=12345] 你好",
                    }
                    req = urllib.request.Request(
                        "http://127.0.0.1:%d/qq/webhook" % port,
                        data=json.dumps(event).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    body = await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=10).read())
                    payload = json.loads(body.decode("utf-8"))
                    self.assertTrue(payload["ok"])
                    await asyncio.sleep(0.5)
                    self.assertEqual(sessions.get_or_create("qq:gqq:777").messages[-1]["content"], "qq ok")
                    self.assertTrue(any(path == "/send_group_msg" for path, _ in onebot.received))
                finally:
                    await channel.stop()
                    await stop_core(bus, loop, tasks)
                    onebot.stop()

        asyncio.run(scenario())

    def test_telegram_allow_list(self):
        channel = TelegramChannel(token="test-token", allow_from=["123", "alice"])

        self.assertTrue(channel._allowed("123", ""))
        self.assertTrue(channel._allowed("999", "Alice"))
        self.assertFalse(channel._allowed("999", "bob"))

    def test_telegram_chunks_long_response(self):
        channel = TelegramChannel(token="test-token")

        chunks = channel._chunks("x" * 4100, 4096)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 4096)
        self.assertEqual(len(chunks[1]), 4)

    def test_qq_api_rejects_failed_retcode(self):
        onebot = FakeOneBotServer(body=b'{"status":"failed","retcode":100,"wording":"bad"}')
        onebot.start()
        try:
            channel = QQChannel(api_base_url="http://127.0.0.1:%d" % onebot.port)
            with self.assertRaises(RuntimeError):
                channel._api("send_private_msg", {"user_id": "1", "message": "hi"})
        finally:
            onebot.stop()


if __name__ == "__main__":
    unittest.main()
