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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from kirakira_agent.event_bus import EventBus
from kirakira_agent.config import load_toml_config
from kirakira_agent.plugin_manifest import (
    MANIFEST_NAME,
    PluginEnablement,
    discover_plugin_roots,
    is_enabled,
    load_manifest,
    normalize_command_item,
    resolve_skill_roots,
    safe_child,
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


def _source_plugin_name(source: str) -> str:
    """插件身份取来源目录名/仓库名；目录名必须与插件 name 一致。"""

    text = source.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return Path(text).name


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
    instance: Optional["Plugin"]

    @property
    def version(self) -> str:
        return str(getattr(self.instance, "version", "") or "")

    @property
    def desc(self) -> str:
        return str(getattr(self.instance, "desc", "") or "")


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


@dataclass(frozen=True)
class McpServerSpec:
    """插件用代码声明的 MCP server；path 一律相对插件根解析。"""

    name: str
    command: tuple[str, ...]
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = "."


class Plugin:
    api_version: int = 1
    name: str = ""
    version: str = ""
    desc: str = ""
    ConfigModel: Any = None

    async def initialize(self) -> None:
        return None

    async def terminate(self) -> None:
        return None

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ()

    @classmethod
    def mcp_servers(cls) -> List[McpServerSpec]:
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
        mcp_publisher: Any = None,
        skill_loader: Any = None,
    ) -> None:
        self.plugin_dirs = plugin_dirs
        self.event_bus = event_bus
        self.tool_registry = tool_registry
        self.workspace = workspace
        self.session_manager = session_manager
        self.memory = memory
        self.mcp_publisher = mcp_publisher
        self.skill_loader = skill_loader
        self.instances: List[Plugin] = []
        self.active: List[ActivePlugin] = []
        self.errors: Dict[str, str] = {}
        self._terminated = False
        self._decorated_modules: Dict[str, List[tuple[int, object]]] = {}
        self._decorated_hooks: List[DecoratedToolHook] = []
        self._register_management_tools()

    async def load_all(self) -> None:
        # 清单只决定启停；损坏时整体失败，不静默退化成“全部启用”。
        manifest = load_manifest(self.workspace / ".kirakira" / MANIFEST_NAME)
        seen_names: set[str] = set()
        for root in discover_plugin_roots(self.plugin_dirs):
            try:
                plugin = self._load_one(root / "plugin.py")
                if plugin is None:
                    raise ValueError("plugin.py declares no Plugin subclass")
                name = str(getattr(plugin, "name", "") or root.name).strip()
                if name in seen_names:
                    logger.warning("duplicate plugin name skipped: %s", name)
                    continue
                seen_names.add(name)
                if not is_enabled(manifest, name):
                    logger.info("plugin disabled by manifest: %s", name)
                    continue
                await self._initialize_plugin(name, root, plugin)
                self.active.append(ActivePlugin(name, root, plugin))
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
        if self.mcp_publisher is not None:
            # 插件 MCP 与 workspace MCP 共用换代语义：整批发布，失败保持旧代际。
            await self.mcp_publisher.publish(self.mcp_servers, source="plugins")

    async def terminate_all(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        for plugin in reversed(self.instances):
            try:
                await plugin.terminate()
            except Exception:
                logger.exception("plugin terminate failed: %s", plugin.name or type(plugin).__name__)
        if self.mcp_publisher is not None:
            await self.mcp_publisher.shutdown()

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
        """把各插件代码声明的 MCP server 规范化成 publisher 需要的 spec。"""

        merged: Dict[str, Dict[str, Any]] = {}
        for record in self.active:
            if record.instance is None:
                continue
            for spec in record.instance.mcp_servers():
                if spec.name in merged:
                    logger.warning("duplicate plugin MCP server skipped: %s", spec.name)
                    continue
                if not spec.command:
                    raise ValueError("plugin MCP server has no command: %s" % spec.name)
                data_dir = self.plugin_data_dir(record.plugin_id)
                env = dict(spec.env)
                env.setdefault("KIRAKIRA_PLUGIN_DATA_DIR", str(data_dir))
                merged[spec.name] = {
                    "command": [
                        normalize_command_item(record.root, item)
                        for item in spec.command
                    ],
                    "cwd": str(safe_child(record.root, spec.cwd or ".")),
                    "env": env,
                }
        return merged

    def plugin_data_dir(self, plugin_id: str) -> Path:
        return self.workspace / ".kirakira" / "plugin-data" / plugin_id

    @staticmethod
    def _validate_declarations(root: Path, plugin: Plugin) -> None:
        """在插件自己的加载边界内校验声明路径，越界立即失败。"""

        resolve_skill_roots(root, plugin.skill_roots())
        for spec in plugin.mcp_servers():
            if not spec.command:
                raise ValueError("plugin MCP server has no command: %s" % spec.name)
            safe_child(root, spec.cwd or ".")
            for item in spec.command:
                normalize_command_item(root, item)

    @property
    def channels(self) -> List[object]:
        channels: List[object] = []
        for plugin in self.instances:
            channels.extend(plugin.channels())
        return channels

    async def _initialize_plugin(self, name: str, root: Path, plugin: Plugin) -> None:
        # 能力声明在这里就校验，坏插件在自己的 try 内失败，不牵连其他插件。
        self._validate_declarations(root, plugin)
        data_dir = self.plugin_data_dir(name)
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
            declared = record.instance.skill_roots() if record.instance else ()
            roots = list(resolve_skill_roots(record.root, declared))
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
        self.tool_registry.register(
            ToolSpec(
                "plugin_enable",
                "Enable an installed plugin in the manifest. Restart is required to load it.",
                object_schema({"name": {"type": "string"}}, ["name"]),
            ),
            self.enable_plugin,
            deferred=True,
        )
        self.tool_registry.register(
            ToolSpec(
                "plugin_disable",
                "Disable an installed plugin in the manifest. Restart is required to unload it.",
                object_schema({"name": {"type": "string"}}, ["name"]),
            ),
            self.disable_plugin,
            deferred=True,
        )
        self.tool_registry.register(
            ToolSpec(
                "plugin_uninstall",
                "Remove an installed plugin directory and its manifest entry. Plugin data is preserved. Restart is required.",
                object_schema({"name": {"type": "string"}}, ["name"]),
            ),
            self.uninstall,
            deferred=True,
        )

    def _manifest_path(self) -> Path:
        return self.workspace / ".kirakira" / MANIFEST_NAME

    @staticmethod
    def _valid_plugin_id(plugin_id: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]*", plugin_id))

    def _write_manifest(self, manifest: Dict[str, PluginEnablement]) -> None:
        path = self._manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        for plugin_id in sorted(manifest):
            # 插件 id 允许 . @ -，会破坏 TOML 裸键，统一用带引号的点号键段。
            lines.append("[plugins.%s]" % json.dumps(plugin_id))
            lines.append("enabled = %s" % ("true" if manifest[plugin_id].enabled else "false"))
            lines.append("")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, path)

    def _set_enabled(self, name: str, enabled: bool) -> str:
        name = name.strip()
        if not self._valid_plugin_id(name):
            return "Error: invalid plugin name: %r" % name
        manifest = load_manifest(self._manifest_path())
        manifest[name] = PluginEnablement(name, enabled)
        self._write_manifest(manifest)
        verb = "enabled" if enabled else "disabled"
        return "Plugin %r %s in manifest. Restart kirakira-agent to apply." % (name, verb)

    def enable_plugin(self, name: str) -> str:
        return self._set_enabled(name, True)

    def disable_plugin(self, name: str) -> str:
        return self._set_enabled(name, False)

    async def uninstall(self, name: str) -> str:
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return "Error: invalid plugin name: %r" % name
        target = self.workspace / ".kirakira" / "plugins" / name
        if not target.is_dir():
            return "Error: plugin %r is not installed" % name
        await asyncio.to_thread(shutil.rmtree, target)
        manifest = load_manifest(self._manifest_path())
        if name in manifest:
            del manifest[name]
            self._write_manifest(manifest)
        return (
            "Uninstalled plugin %r. Data under .kirakira/plugin-data/%s is preserved. "
            "Restart kirakira-agent to apply." % (name, name)
        )

    def list_plugins(self) -> str:
        return json.dumps(
            {
                "active": [
                    {
                        "name": record.plugin_id,
                        "root": str(record.root),
                        "version": record.version,
                        "desc": record.desc,
                        "lifecycle": record.instance is not None,
                        "skills": len(
                            resolve_skill_roots(
                                record.root,
                                record.instance.skill_roots() if record.instance else (),
                            )
                        ),
                        "mcp_servers": sorted(
                            spec.name
                            for spec in (
                                record.instance.mcp_servers() if record.instance else []
                            )
                        ),
                    }
                    for record in self.active
                ],
                "errors": dict(self.errors),
            },
            ensure_ascii=False,
            indent=2,
        )

    def doctor(self, name: str = "") -> str:
        """检查已发现插件的结构，以及已加载插件用代码声明的能力。"""

        loaded = {record.root: record for record in self.active}
        reports = []
        for root in discover_plugin_roots(self.plugin_dirs):
            record = loaded.get(root)
            plugin_name = record.plugin_id if record else root.name
            if name and name not in (plugin_name, root.name):
                continue
            errors: List[str] = []
            warnings: List[str] = []
            if not (root / "plugin.py").is_file():
                errors.append("plugin.py is missing")
            if root.name in self.errors:
                errors.append(self.errors[root.name])
            # 能力声明只有在插件已加载时才可信；未加载的插件不在这里执行其代码。
            if record is None or record.instance is None:
                warnings.append("plugin is not loaded; capability checks skipped")
            else:
                errors.extend(self._check_declared_skills(record, warnings))
                errors.extend(self._check_declared_mcp(record))
            reports.append(
                {
                    "name": plugin_name,
                    "root": str(root),
                    "ok": not errors,
                    "errors": errors,
                    "warnings": warnings,
                }
            )
        return json.dumps({"plugins": reports}, ensure_ascii=False, indent=2)

    def _check_declared_skills(
        self, record: ActivePlugin, warnings: List[str]
    ) -> List[str]:
        assert record.instance is not None
        try:
            roots = resolve_skill_roots(record.root, record.instance.skill_roots())
        except ValueError as exc:
            return [str(exc)]
        for skill_root in roots:
            candidates = (
                [skill_root]
                if (skill_root / "SKILL.md").is_file()
                else [item for item in skill_root.iterdir() if item.is_dir()]
            )
            for candidate in candidates:
                if not (candidate / "SKILL.md").is_file():
                    warnings.append("skill has no SKILL.md: %s" % candidate)
        return []

    def _check_declared_mcp(self, record: ActivePlugin) -> List[str]:
        assert record.instance is not None
        errors: List[str] = []
        for spec in record.instance.mcp_servers():
            if not spec.command:
                errors.append("MCP server has no command: %s" % spec.name)
            try:
                safe_child(record.root, spec.cwd or ".")
            except ValueError as exc:
                errors.append(str(exc))
        return errors

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
            # 插件身份来自来源目录名：安装期不导入 plugin.py，绝不热执行刚下载的代码。
            if not (staging / "plugin.py").is_file():
                return "Error: plugin must contain plugin.py at its root"
            plugin_name = _source_plugin_name(source)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", plugin_name):
                return "Error: invalid plugin name: %s" % plugin_name
            target = install_root / plugin_name
            if target.exists():
                return "Error: plugin %r is already installed" % plugin_name
            git_dir = staging / ".git"
            if git_dir.exists():
                await asyncio.to_thread(shutil.rmtree, git_dir)
            os.replace(staging, target)
            return (
                "Installed plugin %r at %s. Restart kirakira-agent to activate it."
                % (plugin_name, target)
            )
        finally:
            if staging.exists():
                await asyncio.to_thread(shutil.rmtree, staging, True)
