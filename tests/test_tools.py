"""Kirakira Agent learning harness module."""

import tempfile
import unittest
import asyncio
import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kirakira_agent.bus import MessageBus
from kirakira_agent.events import OutboundMessage
from kirakira_agent.schema import ToolCall, ToolSpec
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools.builtins import WorkspaceTools, build_default_registry, safe_path
from kirakira_agent.tools.registry import ToolRegistry


class ToolTests(unittest.TestCase):
    def test_safe_path_blocks_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                safe_path(Path(tmp), "../outside.txt")

    def test_file_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            tools = WorkspaceTools(workdir, SkillLoader(workdir / "skills"))

            self.assertIn("Wrote", tools.write_file("a.txt", "hello\nworld"))
            self.assertEqual(tools.read_file("a.txt"), "hello\nworld")
            self.assertIn("file\ta.txt", tools.list_dir("."))
            self.assertIn("Edited", tools.edit_file("a.txt", "world", "kirakira"))
            self.assertEqual((workdir / "a.txt").read_text(), "hello\nkirakira")

    def test_registry_executes_and_handles_unknown_tool(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec("echo", "Echo text", {"type": "object", "properties": {}, "required": []}),
            lambda text: text,
        )
        ok = registry.execute(ToolCall("1", "echo", {"text": "hi"}))
        missing = registry.execute(ToolCall("2", "missing", {}))

        self.assertEqual(ok.content, "hi")
        self.assertTrue(missing.is_error)
        self.assertIn("Unknown tool", missing.content)

    def test_registry_has_passive_research_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = build_default_registry(Path(tmp))
            names = registry.names()

        self.assertIn("list_dir", names)
        self.assertIn("web_fetch", names)
        self.assertIn("web_search", names)
        self.assertIn("message_push", names)
        self.assertIn("tool_search", names)

    def test_web_fetch_reads_local_http(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                body = b"<html><body><h1>Hello</h1><p>Web Fetch OK</p></body></html>"
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tools = WorkspaceTools(Path(tmp), SkillLoader(Path(tmp) / "skills"))
                text = tools.web_fetch("http://127.0.0.1:%d/" % port)
            self.assertIn("Web Fetch OK", text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_message_push_publishes_outbound(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus()
                registry = build_default_registry(Path(tmp), bus=bus)
                result = await registry.execute_async(
                    ToolCall(
                        "1",
                        "message_push",
                        {"channel": "cli", "chat_id": "c1", "message": "hello"},
                    )
                )
                outbound = await asyncio.wait_for(bus._outbound.get(), timeout=1)
                self.assertEqual(result.content, "已发送")
                self.assertIsInstance(outbound, OutboundMessage)
                self.assertEqual(outbound.content, "hello")

        asyncio.run(scenario())

    def test_tool_search_returns_matching_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = build_default_registry(Path(tmp))
            result = registry.execute(ToolCall("1", "tool_search", {"query": "fetch"}))
            payload = json.loads(result.content)

        self.assertTrue(any(item["name"] == "web_fetch" for item in payload))


if __name__ == "__main__":
    unittest.main()
