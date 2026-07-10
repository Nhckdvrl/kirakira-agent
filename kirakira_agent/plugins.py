"""Small plugin loader for lifecycle modules, tool hooks, tools, and channels."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, List

from kirakira_agent.event_bus import EventBus
from kirakira_agent.tool_hooks import ToolHook
from kirakira_agent.tools.registry import ToolRegistry


class Plugin:
    name: str = ""

    async def initialize(self) -> None:
        return None

    async def terminate(self) -> None:
        return None

    def before_turn_modules(self) -> List[object]:
        return []

    def before_reasoning_modules(self) -> List[object]:
        return []

    def prompt_render_modules(self) -> List[object]:
        return []

    def before_step_modules(self) -> List[object]:
        return []

    def after_step_modules(self) -> List[object]:
        return []

    def after_reasoning_modules(self) -> List[object]:
        return []

    def after_turn_modules(self) -> List[object]:
        return []

    def tool_hooks(self) -> List[ToolHook]:
        return []

    def register_tools(self, registry: ToolRegistry) -> None:
        return None


class PluginManager:
    def __init__(
        self,
        plugin_dirs: List[Path],
        *,
        event_bus: EventBus,
        tool_registry: ToolRegistry,
        workspace: Path,
        session_manager: Any,
        memory: Any,
    ) -> None:
        self.plugin_dirs = plugin_dirs
        self.event_bus = event_bus
        self.tool_registry = tool_registry
        self.workspace = workspace
        self.session_manager = session_manager
        self.memory = memory
        self.instances: List[Plugin] = []

    async def load_all(self) -> None:
        for root in self.plugin_dirs:
            if not root.exists():
                continue
            for path in sorted(root.glob("*/plugin.py")):
                plugin = self._load_one(path)
                if plugin is None:
                    continue
                plugin.context = {  # type: ignore[attr-defined]
                    "event_bus": self.event_bus,
                    "tool_registry": self.tool_registry,
                    "workspace": self.workspace,
                    "session_manager": self.session_manager,
                    "memory": self.memory,
                    "plugin_dir": path.parent,
                }
                plugin.register_tools(self.tool_registry)
                await plugin.initialize()
                self.instances.append(plugin)

    async def terminate_all(self) -> None:
        for plugin in reversed(self.instances):
            await plugin.terminate()

    @property
    def tool_hooks(self) -> List[ToolHook]:
        hooks: List[ToolHook] = []
        for plugin in self.instances:
            hooks.extend(plugin.tool_hooks())
        return hooks

    @property
    def before_turn_modules(self) -> List[object]:
        return self._collect("before_turn_modules")

    @property
    def before_reasoning_modules(self) -> List[object]:
        return self._collect("before_reasoning_modules")

    @property
    def prompt_render_modules(self) -> List[object]:
        return self._collect("prompt_render_modules")

    @property
    def before_step_modules(self) -> List[object]:
        return self._collect("before_step_modules")

    @property
    def after_step_modules(self) -> List[object]:
        return self._collect("after_step_modules")

    @property
    def after_reasoning_modules(self) -> List[object]:
        return self._collect("after_reasoning_modules")

    @property
    def after_turn_modules(self) -> List[object]:
        return self._collect("after_turn_modules")

    def _collect(self, name: str) -> List[object]:
        modules: List[object] = []
        for plugin in self.instances:
            getter = getattr(plugin, name)
            modules.extend(getter())
        return modules

    def _load_one(self, path: Path) -> Plugin | None:
        module_name = "kirakira_plugin_%s" % path.parent.name.replace("-", "_")
        spec = importlib.util.spec_from_file_location(
            module_name,
            path,
            submodule_search_locations=[str(path.parent)],
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        factory = getattr(module, "create_plugin", None)
        if callable(factory):
            return factory()
        for value in module.__dict__.values():
            if isinstance(value, type) and issubclass(value, Plugin) and value is not Plugin:
                return value()
        return None

