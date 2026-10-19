"""MCP stdio client and declarative workspace server tests."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

from core.schema import ToolCall
from plugins.mcp import (
    McpCatalogPublisher,
    McpClient,
    WorkspaceMcpWatcher,
    load_workspace_mcp_declarations,
)
from plugins.tools.registry import ToolMenu, ToolRegistry


def _build_watcher(servers):
    tools = ToolRegistry()
    publisher = McpCatalogPublisher(tools)
    return tools, WorkspaceMcpWatcher(servers, publisher)


SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _declaration(name: str = "fake") -> str:
    return "\n".join(
        [
            "schema_version = 1",
            'name = "%s"' % name,
            'command = ["%s", "%s"]' % (sys.executable, SERVER),
        ]
    )


class McpClientTests(unittest.TestCase):
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


class WorkspaceMcpDeclarationTests(unittest.TestCase):
    def _servers_dir(self, tmp: str) -> Path:
        servers = Path(tmp) / "mcp" / "servers"
        servers.mkdir(parents=True)
        return servers

    def test_revision_tracks_content_not_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            servers = self._servers_dir(tmp)
            (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
            first = load_workspace_mcp_declarations(servers).revision
            (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
            self.assertEqual(load_workspace_mcp_declarations(servers).revision, first)

    def test_watch_path_content_changes_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            servers = self._servers_dir(tmp)
            watched = Path(tmp) / "mcp" / "code.py"
            watched.write_text("v1", encoding="utf-8")
            (servers / "fake.toml").write_text(
                _declaration() + '\nwatch_paths = ["../code.py"]\n', encoding="utf-8"
            )
            first = load_workspace_mcp_declarations(servers).revision
            watched.write_text("v2", encoding="utf-8")
            self.assertNotEqual(
                load_workspace_mcp_declarations(servers).revision, first
            )

    def test_rejects_name_mismatching_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            servers = self._servers_dir(tmp)
            (servers / "fake.toml").write_text(_declaration("other"), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_workspace_mcp_declarations(servers)

    def test_rejects_unknown_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            servers = self._servers_dir(tmp)
            (servers / "fake.toml").write_text(
                _declaration() + '\nbogus = "x"\n', encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_workspace_mcp_declarations(servers)

    def test_rejects_cwd_escaping_safe_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            servers = self._servers_dir(tmp)
            (servers / "fake.toml").write_text(
                _declaration() + '\ncwd = "../../../etc"\n', encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_workspace_mcp_declarations(servers)

    def test_missing_directory_yields_empty_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            declarations = load_workspace_mcp_declarations(
                Path(tmp) / "mcp" / "servers", mcp_root=Path(tmp) / "mcp"
            )
            self.assertEqual(declarations.specs, {})


class WorkspaceMcpWatcherTests(unittest.TestCase):
    def _servers_dir(self, tmp: str) -> Path:
        servers = Path(tmp) / "mcp" / "servers"
        servers.mkdir(parents=True)
        return servers

    def test_publishes_declared_server_tools_into_live_registry(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                servers = self._servers_dir(tmp)
                (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
                store, watcher = _build_watcher(servers)
                try:
                    self.assertTrue(await watcher.reconcile())
                    self.assertIn("mcp_fake__echo", tuple(store.names()))
                    result = await store.execute_async(
                        ToolCall("1", "mcp_fake__echo", {"text": "hello"})
                    )
                    self.assertEqual(result.content, "hello")
                    self.assertEqual(watcher.status()["servers"], ["fake"])
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_mcp_tools_are_deferred_in_model_menu(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                servers = self._servers_dir(tmp)
                (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
                store, watcher = _build_watcher(servers)
                try:
                    await watcher.reconcile()
                    view = ToolMenu(store)
                    self.assertTrue(view.is_deferred("mcp_fake__echo"))
                    # deferred 工具未解锁时不进入可见列表。
                    self.assertNotIn(
                        "mcp_fake__echo",
                        [spec.name for spec in view.visible_specs(set())],
                    )
                    self.assertIn(
                        "mcp_fake__echo",
                        [spec.name for spec in view.visible_specs({"mcp_fake__echo"})],
                    )
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_unchanged_revision_does_not_republish(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                servers = self._servers_dir(tmp)
                (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
                store, watcher = _build_watcher(servers)
                try:
                    self.assertTrue(await watcher.reconcile())
                    generation = watcher.status()["generationId"]
                    self.assertFalse(await watcher.reconcile())
                    self.assertEqual(watcher.status()["generationId"], generation)
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_removing_declaration_drains_server_tools(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                servers = self._servers_dir(tmp)
                (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
                store, watcher = _build_watcher(servers)
                try:
                    await watcher.reconcile()
                    (servers / "fake.toml").unlink()
                    self.assertTrue(await watcher.reconcile())
                    self.assertEqual(tuple(store.names()), ())
                    self.assertEqual(watcher.status()["servers"], [])
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_invalid_candidate_keeps_previous_generation_serving(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                servers = self._servers_dir(tmp)
                (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
                store, watcher = _build_watcher(servers)
                try:
                    await watcher.reconcile()
                    generation = watcher.status()["generationId"]
                    # 第二份声明非法：整批候选必须被拒绝，旧代际继续服务。
                    (servers / "broken.toml").write_text(
                        'schema_version = 1\nname = "broken"\ncommand = []\n',
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        await watcher.reconcile()
                    self.assertIn("mcp_fake__echo", tuple(store.names()))
                    self.assertEqual(watcher.status()["generationId"], generation)

                    # 删掉坏声明后内容回到正在服务的 revision，无需换代。
                    (servers / "broken.toml").unlink()
                    self.assertFalse(await watcher.reconcile())
                    self.assertEqual(watcher.status()["generationId"], generation)

                    # 把坏声明改成合法的，则正常换代并带上新 server。
                    (servers / "second.toml").write_text(
                        _declaration("second"), encoding="utf-8"
                    )
                    self.assertTrue(await watcher.reconcile())
                    self.assertEqual(watcher.status()["servers"], ["fake", "second"])
                    result = await store.execute_async(
                        ToolCall("1", "mcp_second__echo", {"text": "ok"})
                    )
                    self.assertEqual(result.content, "ok")
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_unconnectable_new_server_preserves_unrelated_connection(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                servers = self._servers_dir(tmp)
                (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
                store, watcher = _build_watcher(servers)
                try:
                    await watcher.reconcile()
                    (servers / "dead.toml").write_text(
                        "schema_version = 1\n"
                        'name = "dead"\n'
                        'command = ["%s", "-c", "raise SystemExit(1)"]\n'
                        % sys.executable,
                        encoding="utf-8",
                    )
                    with self.assertRaises(Exception):
                        await watcher.reconcile()
                    # 旧 server 的连接没有被这次失败带走。
                    result = await store.execute_async(
                        ToolCall("1", "mcp_fake__echo", {"text": "still-alive"}),
                    )
                    self.assertEqual(result.content, "still-alive")
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_two_sources_share_one_generation(self):
        """workspace 与插件两个来源共用一代 catalog，互相不会覆盖。"""

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                servers = self._servers_dir(tmp)
                (servers / "fake.toml").write_text(_declaration(), encoding="utf-8")
                store, watcher = _build_watcher(servers)
                publisher = watcher._publisher
                try:
                    await watcher.reconcile()
                    await publisher.publish(
                        {
                            "plugged": {
                                "command": [sys.executable, str(SERVER)],
                                "env": {},
                            }
                        },
                        source="plugins",
                    )
                    self.assertEqual(
                        tuple(store.names()),
                        (
                            "mcp_fake__echo",
                            "mcp_fake__fail",
                            "mcp_plugged__echo",
                            "mcp_plugged__fail",
                        ),
                    )
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()


@pytest.mark.asyncio
async def test_partial_mcp_setup_retains_failed_cleanup_until_explicit_retry(
    monkeypatch,
):
    from plugins.mcp.host import McpGenerationHost

    attempts = []

    class FailedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            raise OSError("handshake failed")

        async def disconnect(self):
            attempts.append("close")
            if len(attempts) == 1:
                raise OSError("process still running")

    monkeypatch.setattr("plugins.mcp.host.McpClient", FailedClient)
    host = McpGenerationHost()
    import pytest

    with pytest.raises(BaseExceptionGroup, match="setup and cleanup"):
        await host.prepare("partial", server_specs={"broken": {"command": ["unused"]}})
    assert host.get("partial") is not None
    await host.close("partial")
    assert host.get("partial") is None and attempts == ["close", "close"]
