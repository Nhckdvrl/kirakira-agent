"""Stable plugin authoring API, independent of the manager implementation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from agent.composition import ResourceScope, ServiceKey, ServiceRegistry
from agent.plugins.jobs import PluginJobSpec
from agent.plugins.registry import plugin_registry
from agent.plugins.specs import (
    ManagedServiceSpec,
    McpServerSpec,
    PluginReadinessContext,
    PluginSemanticCheck,
    ProactiveSourceSpec,
)
from agent.tool_hooks import ToolHook
from agent.tools.registry import ToolRegistry
from bus.event_bus import EventBus


class PluginKVStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, key: str, default: Any = None) -> Any:
        return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def increment(self, key: str, delta: int = 1) -> int:
        data = self._read()
        value = int(data.get(key, 0)) + int(delta)
        data[key] = value
        self._write(data)
        return value

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Plugin KV store must contain a JSON object")
        return payload

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(".%s.%s.tmp" % (self.path.name, uuid4().hex))
        try:
            temp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


@dataclass
class PluginContext:
    event_bus: EventBus
    tool_registry: ToolRegistry
    plugin_id: str
    plugin_dir: Path
    data_dir: Path
    kv_store: PluginKVStore
    workspace: Path
    config: Any = None
    services: ServiceRegistry | None = None
    required_services: tuple = ()
    scope: ResourceScope = field(default_factory=ResourceScope)
    _provided: dict = field(default_factory=dict)
    _published: bool = False

    def provide(self, key: ServiceKey, value):
        if self._published:
            raise RuntimeError("capability declarations are frozen after publication")
        if key in self._provided:
            raise ValueError("duplicate provided capability: " + key.name)
        self._provided[key] = value

    def use(self, key: ServiceKey):
        if self.services is None:
            raise RuntimeError("no capability registry was injected")
        if key.name not in {k.name for k in self.required_services}:
            raise PermissionError("capability dependency was not declared: " + key.name)
        return self.services.use(key)

    def spawn(self, coroutine, *, name=None):
        return self.scope.spawn(coroutine, name=name)

    def on_close(self, closer):
        self.scope.on_close(closer)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class Plugin:
    api_version: int = 3
    name: str = ""
    version: str = ""
    desc: str = ""
    author: str = ""
    ConfigModel: Any = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        # 照 Reference:插件类定义即注册,manager 不必扫描类名。
        super().__init_subclass__(**kwargs)
        plugin_registry.register_class(cls)

    @classmethod
    def requires(cls) -> tuple[ServiceKey, ...]:
        return ()

    async def initialize(self) -> None:
        return None

    async def prepare(self) -> None:
        """Prepare a Runtime API v3 candidate before it becomes visible.

        Existing Kirakira plugins that only implement ``initialize`` keep
        working through this compatibility seam.
        """

        await self.initialize()

    def activate(self) -> None:
        return None

    def retire(self) -> None:
        return None

    async def terminate(self) -> None:
        return None

    # --- 语义检查:能力以运行时为准,检查不过的插件不进入可用代际 ---

    def static_semantic_checks(self) -> List[PluginSemanticCheck]:
        return []

    async def readiness_semantic_checks(
        self,
        context: PluginReadinessContext,
    ) -> List[PluginSemanticCheck]:
        return []

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ()

    @classmethod
    def drift_skill_roots(cls) -> tuple[str, ...]:
        return ()

    @classmethod
    def mcp_servers(cls) -> List[McpServerSpec]:
        return []

    @classmethod
    def managed_services(cls) -> List[ManagedServiceSpec]:
        return []

    def proactive_sources(self) -> List[ProactiveSourceSpec]:
        return []

    def jobs(self) -> List[PluginJobSpec]:
        return []

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

    def channels(self) -> List[object]:
        return []
