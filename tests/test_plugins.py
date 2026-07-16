"""Plugin manifest, lifecycle rollback, skill, and MCP tests."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from kirakira_agent.event_bus import EventBus
from kirakira_agent.plugins import PluginManager
from kirakira_agent.schema import ToolCall
from kirakira_agent.tool_hooks import HookContext, ToolExecutionRequest
from kirakira_agent.tools.registry import ToolRegistry


def write_enablement(workspace, body):
    manifest_dir = workspace / ".kirakira"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "manifest.toml").write_text(body, encoding="utf-8")


class FakeMcpPublisher:
    def __init__(self):
        self.configs = None

    async def publish(self, configs, *, source="workspace"):
        self.configs = configs
        self.source = source
        return "gen-test"

    async def shutdown(self):
        return None


class FakeSkillLoader:
    def __init__(self):
        self.reloads = 0

    def reload(self):
        self.reloads += 1


class PluginTests(unittest.TestCase):
    def test_decorated_phase_tool_and_pre_hook_are_discovered(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                root = workspace / "plugins" / "decorated"
                root.mkdir(parents=True)
                root.joinpath("plugin.py").write_text(
                    "from kirakira_agent.plugins import Plugin, on_before_turn, on_tool_pre, tool\n"
                    "class Decorated(Plugin):\n"
                    "    @on_before_turn(priority=10)\n"
                    "    def touch(self, ctx):\n"
                    "        ctx.extra_metadata['decorated'] = True\n"
                    "        return ctx\n"
                    "    @tool('decorated_echo', always_on=True)\n"
                    "    async def echo(self, event, text: str):\n"
                    "        return event.get('session_key', '') + ':' + text\n"
                    "    @on_tool_pre(tool_name='bash')\n"
                    "    def block_bash(self, event):\n"
                    "        return False\n",
                    encoding="utf-8",
                )
                tools = ToolRegistry()
                tools.set_context(session_key="plugin:test")
                manager = PluginManager(
                    [workspace / "plugins"],
                    event_bus=EventBus(),
                    tool_registry=tools,
                    workspace=workspace,
                    session_manager=None,
                    memory=None,
                )

                await manager.load_all()

                self.assertEqual(len(manager.before_turn_modules), 1)
                result = await tools.execute_async(
                    ToolCall("1", "decorated_echo", {"text": "hello"})
                )
                self.assertEqual(result.content, "plugin:test:hello")
                request = ToolExecutionRequest(
                    "plugin:test", "cli", "test", "bash", {"command": "pwd"}
                )
                hook_result = await manager.tool_hooks[0].run(
                    HookContext("pre_tool_use", request, {"command": "pwd"})
                )
                self.assertEqual(hook_result.decision, "deny")

        asyncio.run(scenario())

    def test_programmatic_capabilities_are_assembled(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                root = workspace / "plugins" / "full"
                (root / "skills" / "hello").mkdir(parents=True)
                (root / "skills" / "hello" / "SKILL.md").write_text(
                    "---\nname: hello\ndescription: hi\n---\nbody", encoding="utf-8"
                )
                # 能力全部由 plugin.py 用代码声明，没有任何描述符文件。
                (root / "plugin.py").write_text(
                    "from kirakira_agent.plugins import McpServerSpec, Plugin\n"
                    "class FullPlugin(Plugin):\n"
                    "    name = 'full'\n"
                    "    version = '1.0.0'\n"
                    "    @classmethod\n"
                    "    def skill_roots(cls):\n"
                    "        return ('skills',)\n"
                    "    @classmethod\n"
                    "    def mcp_servers(cls):\n"
                    "        return [McpServerSpec('demo', ('python', './server.py'),"
                    " {'TOKEN': 'test'})]\n"
                    "    async def initialize(self):\n"
                    "        self.context.kv_store.increment('starts')\n",
                    encoding="utf-8",
                )
                mcp = FakeMcpPublisher()
                skills = FakeSkillLoader()
                manager = PluginManager(
                    [workspace / "plugins"],
                    event_bus=EventBus(),
                    tool_registry=ToolRegistry(),
                    workspace=workspace,
                    session_manager=None,
                    memory=None,
                    mcp_publisher=mcp,
                    skill_loader=skills,
                )

                await manager.load_all()

                self.assertEqual(len(manager.instances), 1)
                self.assertEqual(manager.instances[0].context.kv_store.get("starts"), 1)
                self.assertTrue((workspace / "skills" / "hello").is_symlink())
                self.assertEqual(skills.reloads, 1)
                self.assertIn("demo", mcp.configs)
                self.assertEqual(
                    mcp.configs["demo"]["command"][1], str((root / "server.py").resolve())
                )
                self.assertEqual(mcp.configs["demo"]["env"]["TOKEN"], "test")
                listed = json.loads(manager.list_plugins())["active"][0]
                self.assertEqual(listed["version"], "1.0.0")
                self.assertEqual(listed["mcp_servers"], ["demo"])

        asyncio.run(scenario())

    def test_manifest_disables_plugin(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                root = workspace / "plugins" / "off"
                root.mkdir(parents=True)
                root.joinpath("plugin.py").write_text(
                    "from kirakira_agent.plugins import Plugin\n"
                    "class Off(Plugin):\n"
                    "    name = 'off'\n",
                    encoding="utf-8",
                )
                write_enablement(workspace, '[plugins."off"]\nenabled = false\n')
                manager = PluginManager(
                    [workspace / "plugins"],
                    event_bus=EventBus(),
                    tool_registry=ToolRegistry(),
                    workspace=workspace,
                    session_manager=None,
                    memory=None,
                )

                await manager.load_all()

                self.assertEqual(manager.active, [])

        asyncio.run(scenario())

    def test_corrupt_manifest_fails_loud(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                (workspace / "plugins").mkdir()
                write_enablement(workspace, '[plugins."x"]\nenabled = "yes"\n')
                manager = PluginManager(
                    [workspace / "plugins"],
                    event_bus=EventBus(),
                    tool_registry=ToolRegistry(),
                    workspace=workspace,
                    session_manager=None,
                    memory=None,
                )

                with self.assertRaises(ValueError):
                    await manager.load_all()

        asyncio.run(scenario())

    def test_plugin_install_requires_entrypoint_and_restart(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "workspace"
                source = Path(tmp) / "installed-demo"
                workspace.mkdir()
                (source / "skills" / "demo").mkdir(parents=True)
                (source / "skills" / "demo" / "SKILL.md").write_text("demo")
                (source / "plugin.py").write_text(
                    "from kirakira_agent.plugins import Plugin\n"
                    "class Demo(Plugin):\n"
                    "    name = 'installed-demo'\n",
                    encoding="utf-8",
                )
                manager = PluginManager(
                    [workspace / ".kirakira" / "plugins"],
                    event_bus=EventBus(),
                    tool_registry=ToolRegistry(),
                    workspace=workspace,
                    session_manager=None,
                    memory=None,
                )

                result = await manager.install(str(source))

                self.assertIn("Restart", result)
                self.assertTrue(
                    (workspace / ".kirakira" / "plugins" / "installed-demo").is_dir()
                )
                self.assertEqual(manager.active, [])
                duplicate = await manager.install(str(source))
                self.assertIn("already installed", duplicate)

        asyncio.run(scenario())

    def test_bad_plugin_rolls_back_tools_and_does_not_block_good_plugin(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                plugins = workspace / "plugins"
                bad = plugins / "a_bad"
                good = plugins / "b_good"
                bad.mkdir(parents=True)
                good.mkdir(parents=True)
                bad.joinpath("plugin.py").write_text(
                    "from kirakira_agent.plugins import Plugin\n"
                    "from kirakira_agent.schema import ToolSpec\n"
                    "class Bad(Plugin):\n"
                    "    def register_tools(self, registry):\n"
                    "        registry.register(ToolSpec('leaked', 'x', {'type': 'object'}), lambda: 'x')\n"
                    "    async def initialize(self):\n"
                    "        raise RuntimeError('broken init')\n",
                    encoding="utf-8",
                )
                good.joinpath("plugin.py").write_text(
                    "from kirakira_agent.plugins import Plugin\n"
                    "class Good(Plugin):\n"
                    "    name = 'good'\n",
                    encoding="utf-8",
                )
                tools = ToolRegistry()
                manager = PluginManager(
                    [plugins],
                    event_bus=EventBus(),
                    tool_registry=tools,
                    workspace=workspace,
                    session_manager=None,
                    memory=None,
                )

                with self.assertLogs("kirakira_agent.plugins", level="ERROR"):
                    await manager.load_all()

                self.assertFalse(tools.has("leaked"))
                self.assertEqual([plugin.name for plugin in manager.instances], ["good"])
                self.assertIn("a_bad", manager.errors)

        asyncio.run(scenario())

    def test_declaration_rejects_path_traversal_without_stopping_other_plugins(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                bad = workspace / "plugins" / "bad"
                good = workspace / "plugins" / "good"
                bad.mkdir(parents=True)
                good.mkdir(parents=True)
                bad.joinpath("plugin.py").write_text(
                    "from kirakira_agent.plugins import Plugin\n"
                    "class Bad(Plugin):\n"
                    "    name = 'bad'\n"
                    "    @classmethod\n"
                    "    def skill_roots(cls):\n"
                    "        return ('../../outside',)\n",
                    encoding="utf-8",
                )
                good.joinpath("plugin.py").write_text(
                    "from kirakira_agent.plugins import Plugin\nclass Good(Plugin):\n    pass\n",
                    encoding="utf-8",
                )
                manager = PluginManager(
                    [workspace / "plugins"],
                    event_bus=EventBus(),
                    tool_registry=ToolRegistry(),
                    workspace=workspace,
                    session_manager=None,
                    memory=None,
                )

                with self.assertLogs("kirakira_agent.plugins", level="ERROR"):
                    await manager.load_all()

                self.assertIn("bad", manager.errors)
                self.assertEqual(len(manager.instances), 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
