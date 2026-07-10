"""Small plugin loader for lifecycle modules, tool hooks, tools, and channels."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from kirakira_agent.event_bus import EventBus
from kirakira_agent.config import load_toml_config
from kirakira_agent.plugin_manifest import (
    PluginDescriptor,
    discover_plugin_roots,
    load_plugin_descriptor,
)
from kirakira_agent.plugin_decorators import (
    get_bindings,
    on_after_reasoning,
    on_after_step,
    on_after_turn,
    on_before_reasoning,
    on_before_step,
    on_before_turn,
    on_prompt_render,
    on_tool_pre,
    tool,
)
from kirakira_agent.schema import ToolSpec
from kirakira_agent.tool_hooks import HookContext, HookOutcome, ToolHook
from kirakira_agent.tools.registry import ToolRegistry, object_schema

logger = logging.getLogger(__name__)


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
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
    session_manager: Any
    memory: Any
    config: Any = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass
class ActivePlugin:
    plugin_id: str
    root: Path
    descriptor: Optional[PluginDescriptor]
    instance: Optional["Plugin"]


class DecoratedToolHook:
    event = "pre_tool_use"

    def __init__(self, name: str, method, tool_name: Optional[str], priority: int) -> None:
        self.name = name
        self.method = method
        self.tool_name = tool_name
        self.priority = priority

    def matches(self, ctx: HookContext) -> bool:
        return self.tool_name is None or self.tool_name == ctx.request.tool_name

    async def run(self, ctx: HookContext) -> HookOutcome:
        result = self.method(ctx)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return HookOutcome()
        if isinstance(result, HookOutcome):
            return result
        if isinstance(result, dict):
            return HookOutcome(
                decision="deny" if result.get("decision") == "deny" else "allow",
                updated_input=result.get("updated_input"),
                reason=str(result.get("reason") or ""),
                extra_message=str(result.get("extra_message") or ""),
            )
        if result is False:
            return HookOutcome(decision="deny", reason="blocked by plugin hook")
        return HookOutcome()


class Plugin:
    name: str = ""
    ConfigModel: Any = None

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

    def channels(self) -> List[object]:
        return []


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
        mcp_registry: Any = None,
        skill_loader: Any = None,
    ) -> None:
        self.plugin_dirs = plugin_dirs
        self.event_bus = event_bus
        self.tool_registry = tool_registry
        self.workspace = workspace
        self.session_manager = session_manager
        self.memory = memory
        self.mcp_registry = mcp_registry
        self.skill_loader = skill_loader
        self.instances: List[Plugin] = []
        self.active: List[ActivePlugin] = []
        self.errors: Dict[str, str] = {}
        self._terminated = False
        self._decorated_modules: Dict[str, List[tuple[int, object]]] = {}
        self._decorated_hooks: List[DecoratedToolHook] = []
        self._register_management_tools()

    async def load_all(self) -> None:
        seen_names: set[str] = set()
        for root in discover_plugin_roots(self.plugin_dirs):
            try:
                descriptor = load_plugin_descriptor(root)
                name = descriptor.name if descriptor else root.name
                if name in seen_names:
                    logger.warning("duplicate plugin name skipped: %s", name)
                    continue
                seen_names.add(name)
                entry = descriptor.lifecycle_entry if descriptor else root / "plugin.py"
                plugin = None
                if entry is not None and entry.is_file():
                    plugin = self._load_one(
                        entry,
                        descriptor.lifecycle_class if descriptor else "",
                    )
                if plugin is not None:
                    await self._initialize_plugin(name, root, plugin)
                self.active.append(ActivePlugin(name, root, descriptor, plugin))
            except Exception as exc:
                self.errors[root.name] = str(exc)
                logger.exception("plugin failed to load: %s", root)
        self._sync_skill_links()
        for record in self.active:
            (self.workspace / ".kirakira" / "plugin-data" / record.plugin_id).mkdir(
                parents=True, exist_ok=True
            )
        if self.skill_loader is not None:
            self.skill_loader.reload()
        if self.mcp_registry is not None:
            await self.mcp_registry.sync_plugin_servers(self.mcp_servers)

    async def terminate_all(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        for plugin in reversed(self.instances):
            try:
                await plugin.terminate()
            except Exception:
                logger.exception("plugin terminate failed: %s", plugin.name or type(plugin).__name__)

    @property
    def tool_hooks(self) -> List[ToolHook]:
        hooks: List[ToolHook] = []
        for plugin in self.instances:
            hooks.extend(plugin.tool_hooks())
        hooks.extend(sorted(self._decorated_hooks, key=lambda item: -item.priority))
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
        phase = name.removesuffix("_modules")
        modules.extend(
            module
            for _priority, module in sorted(
                self._decorated_modules.get(phase, []), key=lambda item: -item[0]
            )
        )
        return modules

    @property
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for record in self.active:
            if record.descriptor is None:
                continue
            for name, config in record.descriptor.mcp_servers.items():
                if name in merged:
                    logger.warning("duplicate plugin MCP server skipped: %s", name)
                    continue
                normalized = dict(config)
                env = dict(normalized.get("env") or {})
                data_dir = self.workspace / ".kirakira" / "plugin-data" / record.plugin_id
                env.setdefault("KIRAKIRA_PLUGIN_DATA_DIR", str(data_dir))
                env.setdefault("AKA_PLUGIN_DATA_DIR", str(data_dir))
                normalized["env"] = env
                merged[name] = normalized
        return merged

    @property
    def channels(self) -> List[object]:
        channels: List[object] = []
        for plugin in self.instances:
            channels.extend(plugin.channels())
        return channels

    async def _initialize_plugin(self, name: str, root: Path, plugin: Plugin) -> None:
        data_dir = self.workspace / ".kirakira" / "plugin-data" / name
        context = PluginContext(
            event_bus=self.event_bus,
            tool_registry=self.tool_registry,
            plugin_id=name,
            plugin_dir=root,
            data_dir=data_dir,
            kv_store=PluginKVStore(data_dir / "kv.json"),
            workspace=self.workspace,
            session_manager=self.session_manager,
            memory=self.memory,
            config=self._load_plugin_config(root, plugin),
        )
        plugin.context = context  # type: ignore[attr-defined]
        before = set(self.tool_registry.names())
        pending_modules: Dict[str, List[tuple[int, object]]] = {}
        pending_hooks: List[DecoratedToolHook] = []
        try:
            plugin.register_tools(self.tool_registry)
            self._register_decorated(
                name, plugin, pending_modules=pending_modules, pending_hooks=pending_hooks
            )
            await plugin.initialize()
        except Exception:
            for tool_name in set(self.tool_registry.names()) - before:
                self.tool_registry.unregister(tool_name)
            try:
                await plugin.terminate()
            except Exception:
                logger.exception("plugin rollback terminate failed: %s", name)
            raise
        self.instances.append(plugin)
        for phase, modules in pending_modules.items():
            self._decorated_modules.setdefault(phase, []).extend(modules)
        self._decorated_hooks.extend(pending_hooks)

    @staticmethod
    def _load_plugin_config(root: Path, plugin: Plugin) -> Any:
        merged: Dict[str, Any] = {}
        for filename in ("config.toml", "config.local.toml"):
            payload = load_toml_config(root / filename)
            merged.update(payload)
        config_model = getattr(plugin, "ConfigModel", None)
        if config_model is not None:
            return config_model(**merged)
        return merged

    def _register_decorated(
        self,
        plugin_name: str,
        plugin: Plugin,
        *,
        pending_modules: Dict[str, List[tuple[int, object]]],
        pending_hooks: List[DecoratedToolHook],
    ) -> None:
        seen: set[str] = set()
        for cls in type(plugin).mro():
            for attribute_name, raw_method in cls.__dict__.items():
                if attribute_name in seen:
                    continue
                bindings = get_bindings(raw_method)
                if not bindings:
                    continue
                seen.add(attribute_name)
                method = getattr(plugin, attribute_name)
                for binding in bindings:
                    if binding.kind == "phase":
                        pending_modules.setdefault(binding.phase, []).append(
                            (binding.priority, method)
                        )
                    elif binding.kind == "tool_hook":
                        pending_hooks.append(
                            DecoratedToolHook(
                                "%s.%s" % (plugin_name, attribute_name),
                                method,
                                binding.hook_tool_name,
                                binding.priority,
                            )
                        )
                    elif binding.kind == "tool":
                        self.tool_registry.register(
                            ToolSpec(
                                binding.tool_name,
                                binding.tool_description,
                                dict(binding.tool_schema or {}),
                            ),
                            self._decorated_tool_handler(method),
                            deferred=binding.deferred,
                        )

    def _decorated_tool_handler(self, method):
        parameters = list(inspect.signature(method).parameters)

        async def invoke(**kwargs: Any):
            if parameters and parameters[0] == "event":
                result = method(self.tool_registry.context, **kwargs)
            else:
                result = method(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result

        return invoke

    def _sync_skill_links(self) -> None:
        skills_dir = self.workspace / "skills"
        expected: Dict[str, Path] = {}
        plugin_roots = {record.root.resolve() for record in self.active}
        for record in self.active:
            roots = list(record.descriptor.skill_roots) if record.descriptor else []
            fallback = record.root / "skills"
            if not roots and fallback.is_dir():
                roots.append(fallback)
            for root in roots:
                candidates = [root] if (root / "SKILL.md").is_file() else list(root.iterdir())
                for skill in sorted(candidates):
                    if not skill.is_dir() or not (skill / "SKILL.md").is_file():
                        continue
                    expected.setdefault(skill.name, skill.resolve())
        if expected:
            skills_dir.mkdir(parents=True, exist_ok=True)
        for name, target in expected.items():
            link = skills_dir / name
            if link.is_symlink() and link.resolve() == target:
                continue
            if link.exists() or link.is_symlink():
                logger.warning("plugin skill path collision, keeping existing path: %s", link)
                continue
            link.symlink_to(target, target_is_directory=True)
        if not skills_dir.exists():
            return
        for link in skills_dir.iterdir():
            if not link.is_symlink() or link.name in expected:
                continue
            try:
                target = link.resolve(strict=False)
                managed = any(target == root or root in target.parents for root in plugin_roots)
            except OSError:
                managed = False
            if managed:
                link.unlink()

    def _load_one(self, path: Path, class_name: str = "") -> Plugin | None:
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
        if class_name:
            value = getattr(module, class_name, None)
            if not isinstance(value, type) or not issubclass(value, Plugin):
                raise TypeError("Plugin class not found: %s" % class_name)
            return value()
        for value in module.__dict__.values():
            if isinstance(value, type) and issubclass(value, Plugin) and value is not Plugin:
                return value()
        return None

    def _register_management_tools(self) -> None:
        self.tool_registry.register(
            ToolSpec(
                "plugin_list",
                "List active plugins and plugin load errors.",
                object_schema({}, []),
            ),
            self.list_plugins,
            deferred=True,
        )
        self.tool_registry.register(
            ToolSpec(
                "plugin_doctor",
                "Validate installed plugin manifests, lifecycle entries, skills, and MCP declarations without executing them.",
                object_schema({"name": {"type": "string"}}, []),
            ),
            self.doctor,
            deferred=True,
        )
        self.tool_registry.register(
            ToolSpec(
                "plugin_install",
                "Install an Akashic-compatible plugin from a local directory or HTTPS Git repository. Restart is required.",
                object_schema({"source": {"type": "string"}}, ["source"]),
            ),
            self.install,
            deferred=True,
        )

    def list_plugins(self) -> str:
        return json.dumps(
            {
                "active": [
                    {
                        "name": record.plugin_id,
                        "root": str(record.root),
                        "version": record.descriptor.version
                        if record.descriptor
                        else "",
                        "lifecycle": record.instance is not None,
                        "skills": len(record.descriptor.skill_roots)
                        if record.descriptor
                        else 0,
                        "mcp_servers": sorted(record.descriptor.mcp_servers)
                        if record.descriptor
                        else [],
                    }
                    for record in self.active
                ],
                "errors": dict(self.errors),
            },
            ensure_ascii=False,
            indent=2,
        )

    def doctor(self, name: str = "") -> str:
        reports = []
        for root in discover_plugin_roots(self.plugin_dirs):
            if name and root.name != name:
                descriptor = None
                try:
                    descriptor = load_plugin_descriptor(root)
                except Exception:
                    pass
                if descriptor is None or descriptor.name != name:
                    continue
            errors = []
            warnings = []
            descriptor = None
            try:
                descriptor = load_plugin_descriptor(root)
            except Exception as exc:
                errors.append(str(exc))
            if descriptor is None:
                warnings.append("legacy plugin without .aka-plugin/plugin.json")
                if not (root / "plugin.py").is_file():
                    errors.append("plugin.py is missing")
            else:
                for skill_root in descriptor.skill_roots:
                    candidates = (
                        [skill_root]
                        if (skill_root / "SKILL.md").is_file()
                        else [item for item in skill_root.iterdir() if item.is_dir()]
                    )
                    for candidate in candidates:
                        if not (candidate / "SKILL.md").is_file():
                            warnings.append("skill has no SKILL.md: %s" % candidate)
                for server_name, config in descriptor.mcp_servers.items():
                    command = list(config.get("command") or [])
                    if not command:
                        errors.append("MCP server has no command: %s" % server_name)
            reports.append(
                {
                    "name": descriptor.name if descriptor else root.name,
                    "root": str(root),
                    "ok": not errors,
                    "errors": errors,
                    "warnings": warnings,
                }
            )
        return json.dumps({"plugins": reports}, ensure_ascii=False, indent=2)

    async def install(self, source: str) -> str:
        source = source.strip()
        if not source:
            return "Error: plugin source is empty"
        install_root = self.workspace / ".kirakira" / "plugins"
        install_root.mkdir(parents=True, exist_ok=True)
        staging = install_root / (".install-%s" % uuid4().hex)
        try:
            local = Path(source).expanduser()
            if local.is_dir():
                await asyncio.to_thread(shutil.copytree, local.resolve(), staging)
            else:
                if not source.startswith("https://"):
                    return "Error: remote plugin source must be an HTTPS Git URL"
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--",
                    source,
                    str(staging),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    return "Error: plugin clone timed out"
                if process.returncode:
                    return "Error: plugin clone failed: %s" % output.decode(
                        "utf-8", errors="replace"
                    )[-2000:]
            descriptor = load_plugin_descriptor(staging)
            if descriptor is None:
                return "Error: plugin must contain .aka-plugin/plugin.json"
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", descriptor.name):
                return "Error: invalid plugin name: %s" % descriptor.name
            target = install_root / descriptor.name
            if target.exists():
                return "Error: plugin %r is already installed" % descriptor.name
            git_dir = staging / ".git"
            if git_dir.exists():
                await asyncio.to_thread(shutil.rmtree, git_dir)
            os.replace(staging, target)
            return (
                "Installed plugin %r version %s at %s. Restart kirakira-agent to activate it."
                % (descriptor.name, descriptor.version or "unknown", target)
            )
        finally:
            if staging.exists():
                await asyncio.to_thread(shutil.rmtree, staging, True)
