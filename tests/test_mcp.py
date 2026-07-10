"""MCP stdio integration tests."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from kirakira_agent.mcp import McpClient, McpServerRegistry
from kirakira_agent.schema import ToolCall
from kirakira_agent.tools.registry import ToolRegistry


SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


class McpTests(unittest.TestCase):
    def test_client_handshake_calls_and_errors(self):
        async def scenario():
            client = McpClient("fake", [sys.executable, str(SERVER)])
            try:
                infos = await client.connect()
                self.assertEqual([info.name for info in infos], ["echo", "fail"])
                first, second = await asyncio.gather(
                    client.call("echo", {"text": "one"}),
                    client.call("echo", {"text": "two"}),
                )
                self.assertEqual((first, second), ("one", "two"))
                self.assertTrue((await client.call("fail", {})).startswith("Error:"))
            finally:
                await client.disconnect()

        asyncio.run(scenario())

    def test_registry_registers_persists_and_removes_server_tools(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "mcp_servers.json"
                tools = ToolRegistry()
                registry = McpServerRegistry(config_path, tools)
                try:
                    result = await registry.add(
                        "fake", [sys.executable, str(SERVER)]
                    )
                    self.assertIn("mcp_fake__echo", result)
                    self.assertTrue(tools.has("mcp_fake__echo"))
                    call = await tools.execute_async(
                        ToolCall("1", "mcp_fake__echo", {"text": "hello"})
                    )
                    self.assertEqual(call.content, "hello")
                    payload = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertIn("fake", payload["servers"])

                    await registry.remove("fake")
                    self.assertFalse(tools.has("mcp_fake__echo"))
                finally:
                    await registry.shutdown()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
