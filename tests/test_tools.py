"""Kirakira Agent learning harness module."""

import tempfile
import unittest
import asyncio
import json
import os
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kirakira_agent.bus import MessageBus
from kirakira_agent.events import OutboundMessage
from kirakira_agent.schema import ToolCall, ToolSpec
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools.builtins import WorkspaceTools, build_default_registry, safe_path
from kirakira_agent.tools.registry import ToolRegistry
from kirakira_agent.tool_hooks import ToolExecutionRequest, ToolExecutor


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

    def test_registry_sync_execute_runs_async_tool_when_no_loop(self):
        async def echo_async(text):
            return "async:%s" % text

        registry = ToolRegistry()
        registry.register(
            ToolSpec("echo_async", "Echo text asynchronously", {"type": "object", "properties": {}, "required": []}),
            echo_async,
        )

        result = registry.execute(ToolCall("1", "echo_async", {"text": "hi"}))

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "async:hi")

    def test_registry_context_is_isolated_between_async_tasks(self):
        async def scenario():
            registry = ToolRegistry()

            async def read_context(delay):
                await asyncio.sleep(delay)
                return registry.context.get("session_key", "")

            registry.register(
                ToolSpec(
                    "read_context",
                    "Read task-local context",
                    {"type": "object", "properties": {}, "required": []},
                ),
                read_context,
            )

            async def run_one(session_key, delay):
                token = registry.set_context(session_key=session_key)
                try:
                    return await registry.execute_async(
                        ToolCall(session_key, "read_context", {"delay": delay})
                    )
                finally:
                    registry.reset_context(token)

            first, second = await asyncio.gather(
                run_one("session:first", 0.03),
                run_one("session:second", 0.01),
            )
            self.assertEqual(first.content, "session:first")
            self.assertEqual(second.content, "session:second")

        asyncio.run(scenario())

    def test_sync_tool_handler_does_not_block_event_loop(self):
        async def scenario():
            registry = ToolRegistry()

            def slow():
                time.sleep(0.05)
                return "done"

            registry.register(
                ToolSpec("slow", "Slow", {"type": "object", "properties": {}}),
                slow,
            )
            ticked = []

            async def ticker():
                await asyncio.sleep(0.01)
                ticked.append(True)

            result, _ = await asyncio.gather(
                registry.execute_async(ToolCall("1", "slow", {})), ticker()
            )
            self.assertEqual(result.content, "done")
            self.assertEqual(ticked, [True])

        asyncio.run(scenario())

    def test_pre_hook_failure_fails_closed_without_invoking_tool(self):
        class BrokenHook:
            name = "broken"
            event = "pre_tool_use"

            def matches(self, _ctx):
                return True

            async def run(self, _ctx):
                raise RuntimeError("hook broke")

        async def scenario():
            invoked = []
            executor = ToolExecutor([BrokenHook()])
            request = ToolExecutionRequest("s", "c", "1", "demo", {})

            async def invoke(_name, _args):
                invoked.append(True)
                return "done"

            with self.assertLogs("kirakira_agent.tool_hooks", level="ERROR"):
                result = await executor.execute(request, invoke)
            self.assertEqual(result.status, "error")
            self.assertEqual(invoked, [])

        asyncio.run(scenario())

    def test_registry_marks_error_text_as_failed_result(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec("fails", "Fail", {"type": "object", "properties": {}}),
            lambda: "Error: expected failure",
        )

        result = registry.execute(ToolCall("1", "fails", {}))

        self.assertTrue(result.is_error)

    def test_registry_validates_required_argument_types_before_handler(self):
        called = []
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "typed",
                "Typed tool",
                {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            ),
            lambda count: called.append(count) or "ok",
        )

        missing = registry.execute(ToolCall("1", "typed", {}))
        wrong = registry.execute(ToolCall("2", "typed", {"count": "one"}))

        self.assertTrue(missing.is_error)
        self.assertTrue(wrong.is_error)
        self.assertEqual(called, [])

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
                old_value = os.environ.get("KIRAKIRA_ALLOW_PRIVATE_WEB_FETCH")
                os.environ["KIRAKIRA_ALLOW_PRIVATE_WEB_FETCH"] = "true"
                try:
                    text = tools.web_fetch("http://127.0.0.1:%d/" % port)
                finally:
                    if old_value is None:
                        os.environ.pop("KIRAKIRA_ALLOW_PRIVATE_WEB_FETCH", None)
                    else:
                        os.environ["KIRAKIRA_ALLOW_PRIVATE_WEB_FETCH"] = old_value
            self.assertIn("Web Fetch OK", text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_web_fetch_blocks_local_http_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = WorkspaceTools(Path(tmp), SkillLoader(Path(tmp) / "skills"))
            text = tools.web_fetch("http://127.0.0.1:9/")

        self.assertIn("Refusing to fetch private/local address", text)

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
                outbound, _ticket = await asyncio.wait_for(bus._outbound.get(), timeout=1)
                self.assertEqual(result.content, "已发送")
                self.assertIsInstance(outbound, OutboundMessage)
                self.assertEqual(outbound.content, "hello")

        asyncio.run(scenario())

    def test_background_shell_can_be_polled_and_cleaned(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                registry = build_default_registry(Path(tmp))
                started = await registry.execute_async(
                    ToolCall(
                        "1",
                        "bash",
                        {
                            "command": (
                                "python -c \"import time; print('start', flush=True); "
                                "time.sleep(0.1); print('done')\""
                            ),
                            "run_in_background": True,
                        },
                    )
                )
                task_id = json.loads(started.content)["background_task_id"]
                output = await registry.execute_async(
                    ToolCall(
                        "2",
                        "task_output",
                        {"task_id": task_id, "block": True, "timeout_ms": 2000},
                    )
                )
                payload = json.loads(output.content)
                self.assertTrue(payload["done"])
                self.assertIn("start", payload["output"])
                self.assertIn("done", payload["output"])
                stopped = await registry.execute_async(
                    ToolCall("3", "task_stop", {"task_id": task_id})
                )
                self.assertEqual(json.loads(stopped.content)["status"], "stopped")
                await registry.shutdown()

        asyncio.run(scenario())

    def test_registry_shutdown_kills_background_shell(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                registry = build_default_registry(Path(tmp))
                started = await registry.execute_async(
                    ToolCall(
                        "1",
                        "bash",
                        {
                            "command": "python -c \"import time; time.sleep(30)\"",
                            "run_in_background": True,
                        },
                    )
                )
                task_id = json.loads(started.content)["background_task_id"]
                await asyncio.wait_for(registry.shutdown(), timeout=2.0)
                result = await registry.execute_async(
                    ToolCall("2", "task_output", {"task_id": task_id})
                )
                self.assertTrue(result.is_error)

        asyncio.run(scenario())

    def test_tool_search_returns_matching_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = build_default_registry(Path(tmp))
            result = registry.execute(ToolCall("1", "tool_search", {"query": "fetch"}))
            payload = json.loads(result.content)

        self.assertTrue(any(item["name"] == "web_fetch" for item in payload["matched"]))


if __name__ == "__main__":
    unittest.main()
