"""Agent-managed workspace MCP declaration tests."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from core.schema import ToolCall
from plugins.mcp import McpCatalogPublisher, WorkspaceMcpAdmin, WorkspaceMcpWatcher
from plugins.tools.registry import ToolMenu, ToolRegistry

SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _build(tmp):
    workspace = Path(tmp)
    store = ToolRegistry()
    publisher = McpCatalogPublisher(store)
    watcher = WorkspaceMcpWatcher(workspace / "mcp" / "servers", publisher)
    admin = WorkspaceMcpAdmin(workspace, watcher)
    return workspace, store, watcher, admin


class WorkspaceMcpAdminTests(unittest.TestCase):
    def test_apply_publishes_server_and_writes_declaration(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace, store, watcher, admin = _build(tmp)
                try:
                    result = await admin.apply(
                        name="fake",
                        command=[sys.executable, str(SERVER)],
                    )
                    self.assertEqual(result["status"], "active")
                    self.assertEqual(result["effectiveFrom"], "next_call")

                    # 声明真的落盘了，人手改文件与 agent 写入是同一份真相。
                    path = workspace / "mcp" / "servers" / "fake.toml"
                    self.assertTrue(path.is_file())
                    self.assertIn('name = "fake"', path.read_text(encoding="utf-8"))

                    # 工具立刻在新代际里可用。
                    self.assertIn("mcp_fake__echo", tuple(store.names()))
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_applied_server_is_callable(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _workspace, store, watcher, admin = _build(tmp)
                try:
                    await admin.apply(
                        name="fake", command=[sys.executable, str(SERVER)]
                    )
                    view = ToolMenu(store)
                    result = await view.execute_async(
                        ToolCall("1", "mcp_fake__echo", {"text": "hi"})
                    )
                    self.assertEqual(result.content, "hi")
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_failed_new_server_preserves_selected_declaration_and_unrelated_connection(
        self,
    ):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace, store, watcher, admin = _build(tmp)
                try:
                    await admin.apply(
                        name="fake", command=[sys.executable, str(SERVER)]
                    )
                    generation = watcher.status()["generationId"]

                    # Failed selection stays visible; an unrelated server stays active.
                    with self.assertRaises(RuntimeError):
                        await admin.apply(
                            name="dead",
                            command=[sys.executable, "-c", "raise SystemExit(1)"],
                        )

                    self.assertTrue(
                        (workspace / "mcp" / "servers" / "dead.toml").exists()
                    )
                    self.assertIn("mcp_fake__echo", tuple(store.names()))
                    self.assertEqual(watcher.status()["generationId"], generation)
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_failed_replacement_keeps_new_selection_and_explicit_previous_backup(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace, store, watcher, admin = _build(tmp)
                try:
                    await admin.apply(
                        name="fake", command=[sys.executable, str(SERVER)]
                    )
                    path = workspace / "mcp" / "servers" / "fake.toml"
                    original = path.read_text(encoding="utf-8")

                    # Revoke the old server; keep its content only as an explicit backup.
                    with self.assertRaises(RuntimeError):
                        await admin.apply(
                            name="fake",
                            command=[sys.executable, "-c", "raise SystemExit(1)"],
                        )

                    self.assertNotEqual(path.read_text(encoding="utf-8"), original)
                    self.assertEqual(
                        path.with_suffix(".toml.previous.bak").read_text(), original
                    )
                    self.assertFalse(store.has("mcp_fake__echo"))
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_remove_drains_server(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace, store, watcher, admin = _build(tmp)
                try:
                    await admin.apply(
                        name="fake", command=[sys.executable, str(SERVER)]
                    )
                    result = await admin.remove("fake")

                    self.assertEqual(result["status"], "removed")
                    self.assertFalse(
                        (workspace / "mcp" / "servers" / "fake.toml").exists()
                    )
                    self.assertEqual(tuple(store.names()), ())
                    # 删除前的内容留了备份。
                    self.assertTrue(Path(result["backup"]).is_file())
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_remove_unknown_declaration_fails_loud(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _workspace, _store, watcher, admin = _build(tmp)
                try:
                    with self.assertRaises(ValueError):
                        await admin.remove("nope")
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_status_never_returns_env_values(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _workspace, _store, watcher, admin = _build(tmp)
                try:
                    await admin.apply(
                        name="fake",
                        command=[sys.executable, str(SERVER)],
                        env={"SECRET_TOKEN": "super-secret-value"},
                    )
                    status = admin.status()
                    payload = json.dumps(status, ensure_ascii=False)

                    self.assertIn("SECRET_TOKEN", payload)
                    # 键名可以给模型看，值绝对不能。
                    self.assertNotIn("super-secret-value", payload)
                    self.assertEqual(
                        status["declarations"][0]["envKeys"], ["SECRET_TOKEN"]
                    )
                    self.assertEqual(status["declarations"][0]["state"], "active")
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_status_reports_invalid_declaration(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace, _store, watcher, admin = _build(tmp)
                try:
                    servers = workspace / "mcp" / "servers"
                    servers.mkdir(parents=True)
                    (servers / "broken.toml").write_text("{not toml", encoding="utf-8")

                    entry = admin.status()["declarations"][0]
                    self.assertEqual(entry["state"], "invalid")
                    self.assertTrue(entry["error"])
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_rejects_invalid_names_and_specs(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _workspace, _store, watcher, admin = _build(tmp)
                try:
                    for bad_name in ("Fake", "9fake", "fa/ke", "", "../escape"):
                        with self.assertRaises(ValueError):
                            await admin.apply(name=bad_name, command=["echo"])
                    with self.assertRaises(ValueError):
                        await admin.apply(name="fake", command=[])
                    with self.assertRaises(ValueError):
                        await admin.apply(name="fake", command=["echo"], env={"K": 1})
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())

    def test_rendered_declaration_round_trips_through_loader(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace, store, watcher, admin = _build(tmp)
                try:
                    # watch_paths 必须落在 workspace/mcp/ 安全根内。
                    watched = workspace / "mcp" / "code.py"
                    watched.parent.mkdir(parents=True, exist_ok=True)
                    watched.write_text("v1", encoding="utf-8")

                    # 渲染出的 TOML 必须能被严格的声明加载器接受，否则 agent 写完
                    # 自己就把 watcher 卡死了。
                    await admin.apply(
                        name="fake",
                        command=[sys.executable, str(SERVER)],
                        env={"A": "1"},
                        watch_paths=["../code.py"],
                    )
                    self.assertIn("mcp_fake__echo", tuple(store.names()))
                finally:
                    await watcher.shutdown()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
