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


def write_manifest(root, payload):
    manifest_dir = root / ".aka-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


class FakeMcpRegistry:
    def __init__(self):
        self.configs = None

    async def sync_plugin_servers(self, configs):
        self.configs = configs


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

    def test_manifest_lifecycle_skills_and_mcp_are_assembled(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                root = workspace / "plugins" / "full"
                (root / "skills" / "hello").mkdir(parents=True)
                (root / "skills" / "hello" / "SKILL.md").write_text(
                    "---\nname: hello\ndescription: hi\n---\nbody", encoding="utf-8"
                )
                (root / "mcp").mkdir()
                (root / "mcp" / "servers.json").write_text(
                    json.dumps(
                        {
                            "servers": {
                                "demo": {
                                    "command": ["python", "./server.py"],
                                    "env": {"TOKEN": "test"},
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (root / "plugin.py").write_text(
                    "from kirakira_agent.plugins import Plugin\n"
                    "class FullPlugin(Plugin):\n"
                    "    name = 'full'\n"
                    "    async def initialize(self):\n"
                    "        self.context.kv_store.increment('starts')\n",
                    encoding="utf-8",
                )
                write_manifest(
                    root,
                    {
                        "name": "full",
                        "version": "1.0.0",
                        "paths": {
                            "skills": ["skills"],
                            "mcp_servers": ["mcp/servers.json"],
                        },
                        "akashic": {
                            "lifecycle": {
                                "entry": "plugin.py",
                                "class": "FullPlugin",
                            }
                        },
                    },
                )
                mcp = FakeMcpRegistry()
                skills = FakeSkillLoader()
                manager = PluginManager(
                    [workspace / "plugins"],
                    event_bus=EventBus(),
                    tool_registry=ToolRegistry(),
                    workspace=workspace,
                    session_manager=None,
                    memory=None,
                    mcp_registry=mcp,
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

        asyncio.run(scenario())

    def test_plugin_install_validates_manifest_and_requires_restart(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "workspace"
                source = Path(tmp) / "source"
                workspace.mkdir()
                (source / "skills" / "demo").mkdir(parents=True)
                (source / "skills" / "demo" / "SKILL.md").write_text("demo")
                write_manifest(
                    source,
                    {
                        "name": "installed-demo",
                        "version": "1.2.3",
                        "paths": {"skills": ["skills"]},
                    },
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

    def test_manifest_rejects_path_traversal_without_stopping_other_plugins(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                bad = workspace / "plugins" / "bad"
                good = workspace / "plugins" / "good"
                bad.mkdir(parents=True)
                good.mkdir(parents=True)
                write_manifest(
                    bad,
                    {
                        "name": "bad",
                        "akashic": {"lifecycle": {"entry": "../../outside.py"}},
                    },
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
