"""Small plugin loader for lifecycle modules, tool hooks, tools, and channels."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from agent.config import load_toml_config
from agent.lifecycle.phase import inspect_phase, topo_sort_modules
from core.schema import ToolSpec
from plugins.extensions.api import Plugin, PluginContext, PluginKVStore
from plugins.extensions.decorators import get_bindings
from plugins.extensions.jobs import PluginJobHost, PluginJobSpec
from plugins.extensions.manifest import (
    MANIFEST_NAME,
    PluginEnablement,
    discover_plugin_roots,
    is_enabled,
    load_manifest,
    normalize_command_item,
    resolve_skill_roots,
    safe_child,
)
from plugins.extensions.service_host import PluginServiceHost
from plugins.extensions.specs import (
    PluginReadinessContext,
    ProactiveSourceSpec,
    RegisteredProactiveSource,
    proactive_source_key,
)
from plugins.tools.hooks import HookContext, HookOutcome
from plugins.tools.registry import ToolRegistry, object_schema

logger = logging.getLogger(__name__)


def _path_content_digest(path: Path) -> bytes:
    """按内容取指纹;文件缺失返回固定标记,让"删除"本身也算一次变化。"""
    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        return b"\x00missing"


def _plugin_source_revision(root: Path) -> str:
    """按插件包的全部可分发源码/资源取指纹。

    MCP server、Drift skill 和脚本都是插件行为的一部分；只哈希
    ``plugin.py`` 会让这些文件修改后运行时继续使用旧代。
    """
    digest = hashlib.sha256()
    if not root.exists():
        return ""
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.name in {"config.toml", "config.local.toml"}:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(_path_content_digest(path))
    return digest.hexdigest()[:16]


def _plugin_config_revision(root: Path, data_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in (
        root / "config.toml",
        root / "config.local.toml",
        data_dir / "config.local.toml",
    ):
        digest.update(str(path.name).encode("utf-8"))
        digest.update(_path_content_digest(path))
    return digest.hexdigest()[:16]


def _source_plugin_name(source: str) -> str:
    """插件身份取来源目录名/仓库名；目录名必须与插件 name 一致。"""

    text = source.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return Path(text).name


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

    def __init__(
        self, name: str, method, tool_name: Optional[str], priority: int
    ) -> None:
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


from agent.composition import CompositionRoot, Module


class BoundContribution:
    """Borrow the exact activation for one invocation, never for a whole turn."""

    def __init__(self, owner, value):
        self.owner, self.value = owner, value

    def __getattr__(self, name):
        return getattr(self.value, name)

    async def __call__(self, *args, **kwargs):
        self.owner.acquire()
        try:
            result = self.value(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        finally:
            self.owner.release()

    async def run(self, *args, **kwargs):
        self.owner.acquire()
        try:
            runner = getattr(self.value, "run", self.value)
            result = runner(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        finally:
            self.owner.release()


class PluginManager:
    """Installed class plugins adapt to the same live graph as distribution owners."""

    def __init__(
        self,
        plugin_dirs,
        *,
        event_bus,
        tool_registry,
        workspace,
        mcp_publisher=None,
        skill_loader=None,
        service_registry=None,
        composition=None,
        activation_context=None,
    ):
        self.plugin_dirs, self.event_bus = plugin_dirs, event_bus
        self.tool_registry, self.workspace = tool_registry, workspace
        self.mcp_publisher, self.skill_loader = mcp_publisher, skill_loader
        self.root = composition or CompositionRoot(services=service_registry)
        self.services = self.root.services
        self._owns_root = composition is None
        self._activation_context = activation_context
        self._records = {}
        self.errors = {}
        self.reload_hook = None
        self._reconcile_lock = asyncio.Lock()
        self._terminated = False
        before = set(self.tool_registry.names())
        self._register_management_tools()
        self._management_tools = set(self.tool_registry.names()) - before

    def _withdraw_management_tools(self):
        for name in self._management_tools:
            self.tool_registry.unregister(name)
        self._management_tools.clear()

    @property
    def active(self):
        return [
            record
            for name, record in self._records.items()
            if (node := self.root.activation("plugin:" + name)) is not None
            and node.state == "active"
        ]

    @property
    def instances(self):
        return [r.instance for r in self.active]

    def status(self):
        return tuple(
            row for row in self.root.catalog() if row["name"].startswith("plugin:")
        )

    async def load_all(self):
        if self._owns_root and not self.root.catalog():
            await self.root.start(())
        await self.reconcile_changed()

    def _module(self, name, directory):
        # Loading current source creates no resource or candidate runtime.
        plugin = self._load_one(directory / "plugin.py")
        if plugin is None:
            raise ValueError("plugin.py declares no plugin")
        if plugin.api_version != 3:
            raise ValueError("plugin Runtime API version must be 3")
        self._validate_declarations(directory, plugin)
        source_revision = _plugin_source_revision(directory)
        config_revision = _plugin_config_revision(directory, self.plugin_data_dir(name))

        async def apply(ctx):
            plugin = self._load_one(directory / "plugin.py")
            record = ActivePlugin(name, directory, plugin)
            self._records[name] = record
            data_dir = self.plugin_data_dir(name)
            data_dir.mkdir(parents=True, exist_ok=True)
            context = PluginContext(
                event_bus=self.event_bus,
                tool_registry=self.tool_registry,
                plugin_id=name,
                plugin_dir=directory,
                data_dir=data_dir,
                kv_store=PluginKVStore(data_dir / "kv.json"),
                workspace=self.workspace,
                config=self._load_plugin_config(directory, data_dir, plugin),
                services=self.services,
                required_services=tuple(plugin.requires()),
                scope=ctx._scope,
            )
            plugin.context = context
            context._graph_context = ctx
            ctx.config = context.config
            context._source_revision = source_revision
            context._config_revision = config_revision
            context._phase_modules = {}
            context._hooks = []
            ctx.on_close(lambda: sys.modules.pop(plugin._loaded_module_name, None))
            ctx.on_close(lambda: plugin.terminate())
            staging = ToolRegistry()
            staging._context = self.tool_registry._context
            ctx.on_close(staging.shutdown)
            plugin.register_tools(staging)
            seen = set()
            for cls in type(plugin).mro():
                for attribute, raw in cls.__dict__.items():
                    if attribute in seen:
                        continue
                    seen.add(attribute)
                    bindings = get_bindings(raw)
                    if not bindings:
                        continue
                    method = getattr(plugin, attribute)
                    for binding in bindings:
                        if binding.kind == "phase":
                            context._phase_modules.setdefault(binding.phase, []).append(
                                (binding.priority, method)
                            )
                        elif binding.kind == "tool_hook":
                            context._hooks.append(
                                DecoratedToolHook(
                                    f"{name}.{attribute}",
                                    method,
                                    binding.hook_tool_name,
                                    binding.priority,
                                )
                            )
                        elif binding.kind == "tool":
                            staging.register(
                                ToolSpec(
                                    binding.tool_name,
                                    binding.tool_description,
                                    dict(binding.tool_schema or {}),
                                ),
                                self._decorated_tool_handler(method),
                                deferred=binding.deferred,
                                source_type="plugin",
                                source_name=name,
                            )
            await plugin.prepare()
            checks = list(plugin.static_semantic_checks())
            checks.extend(
                await plugin.readiness_semantic_checks(
                    PluginReadinessContext(
                        workspace_tool_names=tuple(
                            set(self.tool_registry.names()) | set(staging.names())
                        ),
                        mcp_server_names=tuple(self.mcp_servers),
                    )
                )
            )
            failed = [c for c in checks if not c.passed]
            if failed:
                raise ValueError(
                    "plugin readiness failed: " + "; ".join(c.detail for c in failed)
                )
            # Resource registration is an effect of this activation. Revoke its
            # menu before disposal; already admitted calls retain this owner.
            self.tool_registry.install_owner(ctx._activation, staging)
            ctx.on_close(lambda: self.tool_registry.remove_owner(ctx._activation))
            for key, value in context._provided.items():
                ctx.provide(key, value)
            context._published = True
            plugin.activate()
            ctx.on_stopping(plugin.retire)
            job_host = PluginJobHost(event_bus=self.event_bus)
            for spec in plugin.jobs():
                if not isinstance(spec, PluginJobSpec):
                    raise ValueError("jobs() must return PluginJobSpec")
                job_host.register(name, spec)
            ctx.on_close(job_host.aclose)
            ctx.on_stopping(job_host.aclose)
            job_host.start()
            service_host = PluginServiceHost()
            # Only this plugin's processes are owned and restarted here.
            service_host.bind_plugin_services(self._services_for(record))
            ctx.on_close(service_host.stop_all)
            await service_host.start_all()
            if self.mcp_publisher is not None:
                await self.mcp_publisher.publish(
                    self._mcp_for(record), source="plugin:" + name
                )
                ctx.on_close(
                    lambda: self.mcp_publisher.publish({}, source="plugin:" + name)
                )

        config = self._load_plugin_config(directory, self.plugin_data_dir(name), None)
        module = Module(
            "plugin:" + name,
            apply,
            tuple(plugin.requires()),
            None,
            {**config, "workspace": self.workspace, "plugin_dir": directory},
        )
        sys.modules.pop(plugin._loaded_module_name, None)
        return module

    async def reconcile_changed(self):
        async with self._reconcile_lock:
            manifest = load_manifest(self._manifest_path())
            desired = {
                p.name: p
                for p in discover_plugin_roots(self.plugin_dirs)
                if is_enabled(manifest, p.name)
            }
            results = []
            for name in sorted(set(self._records) - set(desired)):
                if self.root.activation("plugin:" + name) is not None:
                    await self.root.remove("plugin:" + name)
                self._records.pop(name, None)
                results.append({"plugin_id": name, "state": "deactivated"})
            pending = dict(desired)
            while pending:
                progress = False
                for name, directory in tuple(pending.items()):
                    old = self._records.get(name)
                    if old is not None:
                        context = old.instance.context
                        node = self.root.activation("plugin:" + name)
                        if (
                            node is not None
                            and node.state == "active"
                            and (
                                context._source_revision
                                == _plugin_source_revision(directory)
                                and context._config_revision
                                == _plugin_config_revision(
                                    directory, self.plugin_data_dir(name)
                                )
                            )
                        ):
                            pending.pop(name)
                            progress = True
                            continue
                    try:
                        module = self._module(name, directory)
                        if any(not self.services.contains(k) for k in module.inject):
                            continue
                        if self._activation_context is not None:
                            module = replace(
                                module, parent=self._activation_context.fiber.name
                            )
                            if (
                                fiber := self.root._fibers.get(module.name)
                            ) is not None and fiber.state.value != "disposed":
                                await self.root.replace(module.name, module)
                                state = "swapped"
                            else:
                                await self.root._mount(
                                    module, parent=self._activation_context
                                )
                                state = "activated"
                        elif "plugin:" + name in self.root._modules:
                            module = replace(
                                module,
                                parent=self.root._modules["plugin:" + name].parent,
                            )
                            await self.root.replace("plugin:" + name, module)
                            state = "swapped"
                        else:
                            await self.root.install(module)
                            state = "activated"
                        self.errors.pop(name, None)
                        results.append({"plugin_id": name, "state": state})
                    except Exception as error:
                        self.errors[name] = str(error)
                        logger.exception("plugin activation failed: %s", name)
                        results.append({"plugin_id": name, "state": "activate_failed"})
                    pending.pop(name)
                    progress = True
                if not progress:
                    for name in pending:
                        self.errors[name] = (
                            "missing required capabilities or dependency cycle"
                        )
                    break
            self._sync_skill_links()
            if self.skill_loader is not None:
                self.skill_loader.reload()
            return results

    async def terminate_all(self):
        if self._terminated:
            return
        if self._owns_root:
            await self.root.close()
        # In the application the common graph owns plugin disposal, including
        # failures. The manager never runs a second lifecycle beside it.
        self._withdraw_management_tools()
        self._terminated = True

    def _collect(self, name):
        modules = []
        phase = name.removesuffix("_modules")
        for record in self.active:
            context = record.instance.context
            node = self.root.activation("plugin:" + record.plugin_id)
            values = list(getattr(record.instance, name)())
            values.extend(
                v
                for _, v in sorted(
                    context._phase_modules.get(phase, []), key=lambda x: -x[0]
                )
            )
            modules.extend(BoundContribution(node, v) for v in values)
        return self._order_phase_modules(phase, modules)

    @property
    def tool_hooks(self):
        result = []
        for record in self.active:
            node = self.root.activation("plugin:" + record.plugin_id)
            values = list(record.instance.tool_hooks()) + record.instance.context._hooks
            result.extend(BoundContribution(node, v) for v in values)
        return result

    @property
    def before_turn_modules(self):
        return self._collect("before_turn_modules")

    @property
    def before_reasoning_modules(self):
        return self._collect("before_reasoning_modules")

    @property
    def prompt_render_modules(self):
        return self._collect("prompt_render_modules")

    @property
    def before_step_modules(self):
        return self._collect("before_step_modules")

    @property
    def after_step_modules(self):
        return self._collect("after_step_modules")

    @property
    def after_reasoning_modules(self):
        return self._collect("after_reasoning_modules")

    @property
    def after_turn_modules(self):
        return self._collect("after_turn_modules")

    def _mcp_for(self, record):
        merged = {}
        for spec in record.instance.mcp_servers():
            env = dict(spec.env)
            env.setdefault(
                "KIRAKIRA_PLUGIN_DATA_DIR", str(self.plugin_data_dir(record.plugin_id))
            )
            merged[spec.name] = {
                "command": [
                    normalize_command_item(record.root, v) for v in spec.command
                ],
                "cwd": str(safe_child(record.root, spec.cwd or ".")),
                "env": env,
            }
        return merged

    def _services_for(self, record):
        services = {}
        for spec in record.instance.managed_services():
            if spec.id in services:
                raise ValueError("duplicate managed service: " + spec.id)
            env = dict(spec.env)
            env.setdefault(
                "KIRAKIRA_PLUGIN_DATA_DIR", str(self.plugin_data_dir(record.plugin_id))
            )
            services[spec.id] = {
                "command": [
                    normalize_command_item(record.root, v) for v in spec.command
                ],
                "cwd": str(safe_child(record.root, spec.cwd or ".")),
                "env": env,
                "readiness_url": spec.readiness_url,
                "startup_timeout_seconds": spec.startup_timeout_seconds,
            }
        return {record.plugin_id: services}

    def watch_revision(self):
        digest = hashlib.sha256()
        digest.update(_path_content_digest(self._manifest_path()))
        for directory in discover_plugin_roots(self.plugin_dirs):
            digest.update(directory.name.encode())
            digest.update(_plugin_source_revision(directory).encode())
            digest.update(
                _plugin_config_revision(
                    directory, self.plugin_data_dir(directory.name)
                ).encode()
            )
        return digest.hexdigest()

    def _request_reload(self):
        if self.reload_hook is not None:
            self.reload_hook()

    async def reconcile_disabled_and_drain(self, plugin_id, *, timeout=30):
        if plugin_id not in {r.plugin_id for r in self.active}:
            raise ValueError("plugin is not loaded: " + plugin_id)
        self.disable_plugin(plugin_id)
        # Cancellation does not claim disposal before its physical owner settles.
        await self.reconcile_changed()
        return "插件已停用并排空: " + plugin_id

    async def uninstall(self, name):
        if not self._valid_plugin_id(name):
            return "Error: invalid plugin name"
        target = self.workspace / ".kirakira" / "plugins" / name
        if not target.is_dir():
            return "Error: plugin is not installed"
        self.disable_plugin(name)
        await self.reconcile_changed()
        from utils.async_tasks import run_sync_drained

        await run_sync_drained(shutil.rmtree, target)
        return f"Uninstalled plugin {name!r}. Plugin data is preserved."

    def _load_one(self, path):
        module_name = (
            "kirakira_plugin_" + path.parent.name.replace("-", "_") + "_" + uuid4().hex
        )
        spec = importlib.util.spec_from_file_location(
            module_name, path, submodule_search_locations=[str(path.parent)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
            native_apply = getattr(module, "apply", None)
            if callable(native_apply):

                class ModulePlugin(Plugin):
                    name = str(getattr(module, "name", path.parent.name))
                    version = str(getattr(module, "version", ""))
                    desc = str(getattr(module, "desc", ""))
                    api_version = getattr(module, "api_version", 3)

                    @classmethod
                    def requires(cls):
                        return tuple(getattr(module, "inject", ()))

                    async def initialize(self):
                        result = native_apply(self.context._graph_context.native)
                        if inspect.isawaitable(result):
                            await result

                plugin = ModulePlugin()
                plugin._loaded_module_name = module_name
                return plugin
            factory = getattr(module, "create_plugin", None)
            if callable(factory):
                plugin = factory()
                plugin._loaded_module_name = module_name
                return plugin
            classes = [
                v
                for v in module.__dict__.values()
                if isinstance(v, type) and issubclass(v, Plugin) and v is not Plugin
            ]
            if classes:
                plugin = classes[0]()
                plugin._loaded_module_name = module_name
                return plugin
            sys.modules.pop(module_name, None)
            return None
        except BaseException:
            sys.modules.pop(module_name, None)
            raise

    @staticmethod
    def _order_phase_modules(phase: str, modules: List[object]) -> List[object]:
        """全部模块都声明了 slot 时按依赖图排序,否则保持注册/优先级顺序。

        只在"全员声明"时启用是刻意的:混用会让未声明 slot 的老模块被隐式重排,
        那种偶然的顺序变化比顺序不可控更难排查。插件全部迁移到 slot 后自动生效。
        """
        if not modules or not all(
            isinstance(getattr(module, "slot", None), str) and getattr(module, "slot")
            for module in modules
        ):
            return modules
        try:
            return topo_sort_modules(modules)
        except RuntimeError as error:
            # 依赖成环/重复 slot 是插件的声明错误:保持原顺序并大声报错,
            # 不能让一个坏插件把整个相位打挂。
            logger.error("phase %s slot ordering failed: %s", phase, error)
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

    @property
    def proactive_sources(self) -> List[RegisteredProactiveSource]:
        """收集插件声明的主动数据源。编译成真实 source 由 proactive.mcp_sources 负责。"""

        sources: List[RegisteredProactiveSource] = []
        seen: set[str] = set()
        for record in self.active:
            if record.instance is None:
                continue
            for spec in record.instance.proactive_sources():
                if not isinstance(spec, ProactiveSourceSpec):
                    raise ValueError(
                        "插件 %s.proactive_sources 返回值不是 ProactiveSourceSpec"
                        % record.plugin_id
                    )
                registered = RegisteredProactiveSource(
                    plugin_id=record.plugin_id, spec=spec
                )
                key = proactive_source_key(registered)
                if key in seen:
                    logger.warning("duplicate plugin proactive source skipped: %s", key)
                    continue
                seen.add(key)
                sources.append(registered)
        return sources

    @property
    def managed_services(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """收集插件声明的长驻服务,规范化成 PluginServiceHost 需要的 binding。

        命令与 cwd 都按插件根解析并做越界校验,和 MCP 声明同一套安全边界。
        """

        merged: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for record in self.active:
            if record.instance is None:
                continue
            for spec in record.instance.managed_services():
                if not spec.command:
                    raise ValueError(
                        "plugin managed service has no command: %s" % spec.id
                    )
                plugin_services = merged.setdefault(record.plugin_id, {})
                if spec.id in plugin_services:
                    logger.warning(
                        "duplicate plugin managed service skipped: %s:%s",
                        record.plugin_id,
                        spec.id,
                    )
                    continue
                env = dict(spec.env)
                env.setdefault(
                    "KIRAKIRA_PLUGIN_DATA_DIR",
                    str(self.plugin_data_dir(record.plugin_id)),
                )
                plugin_services[spec.id] = {
                    "command": [
                        normalize_command_item(record.root, item)
                        for item in spec.command
                    ],
                    "cwd": str(safe_child(record.root, spec.cwd or ".")),
                    "env": env,
                    "readiness_url": spec.readiness_url,
                    "startup_timeout_seconds": spec.startup_timeout_seconds,
                }
        return merged

    @property
    def drift_skill_roots(self) -> List[Path]:
        roots: List[Path] = []
        for record in self.active:
            if record.instance is None:
                continue
            roots.extend(
                resolve_skill_roots(record.root, record.instance.drift_skill_roots())
            )
        return roots

    @staticmethod
    def _load_plugin_config(root: Path, data_dir: Path, plugin: Plugin) -> Any:
        merged: Dict[str, Any] = {}
        # 插件包只携带可分发默认值；用户私有配置由 plugin-data 拥有。
        # 根目录 config.local.toml 仅作旧插件兼容，plugin-data 始终最后覆盖。
        for path in (
            root / "config.toml",
            root / "config.local.toml",
            data_dir / "config.local.toml",
        ):
            payload = load_toml_config(path)
            merged.update(payload)
        config_model = getattr(plugin, "ConfigModel", None)
        if config_model is not None:
            return config_model(**merged)
        return merged

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
                candidates = (
                    [root] if (root / "SKILL.md").is_file() else list(root.iterdir())
                )
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
                logger.warning(
                    "plugin skill path collision, keeping existing path: %s", link
                )
                continue
            link.symlink_to(target, target_is_directory=True)
        if not skills_dir.exists():
            return
        for link in skills_dir.iterdir():
            if not link.is_symlink() or link.name in expected:
                continue
            try:
                target = link.resolve(strict=False)
                managed = any(
                    target == root or root in target.parents for root in plugin_roots
                )
            except OSError:
                managed = False
            if managed:
                link.unlink()

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
                "Install an Akashic-compatible plugin from a local directory or HTTPS Git repository. Applied by hot reload, no restart needed.",
                object_schema({"source": {"type": "string"}}, ["source"]),
            ),
            self.install,
            deferred=True,
        )
        self.tool_registry.register(
            ToolSpec(
                "plugin_enable",
                "Enable an installed plugin in the manifest. Applied by hot reload, no restart needed.",
                object_schema({"name": {"type": "string"}}, ["name"]),
            ),
            self.enable_plugin,
            deferred=True,
        )
        self.tool_registry.register(
            ToolSpec(
                "plugin_disable",
                "Disable an installed plugin in the manifest. Applied by hot reload, no restart needed.",
                object_schema({"name": {"type": "string"}}, ["name"]),
            ),
            self.disable_plugin,
            deferred=True,
        )
        self.tool_registry.register(
            ToolSpec(
                "plugin_uninstall",
                "Remove an installed plugin directory and its manifest entry. Plugin data is preserved. Applied by hot reload.",
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
            lines.append(
                "enabled = %s" % ("true" if manifest[plugin_id].enabled else "false")
            )
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
        self._request_reload()
        return "Plugin %r %s in manifest. Hot reload will apply it shortly." % (
            name,
            verb,
        )

    def enable_plugin(self, name: str) -> str:
        return self._set_enabled(name, True)

    def disable_plugin(self, name: str) -> str:
        return self._set_enabled(name, False)

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
                                record.instance.skill_roots()
                                if record.instance
                                else (),
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
        return json.dumps(
            {"plugins": reports, "phases": self.phase_report()},
            ensure_ascii=False,
            indent=2,
        )

    def phase_report(self) -> Dict[str, str]:
        """各相位的实际执行顺序与依赖关系,用于排查"插件为什么没按预期顺序跑"。"""

        report: Dict[str, str] = {}
        for phase in (
            "before_turn",
            "before_reasoning",
            "prompt_render",
            "before_step",
            "after_step",
            "after_reasoning",
            "after_turn",
        ):
            modules = self._collect("%s_modules" % phase)
            if not modules:
                continue
            if all(getattr(module, "slot", None) for module in modules):
                try:
                    report[phase] = inspect_phase(modules)
                except RuntimeError as error:
                    report[phase] = "slot ordering failed: %s" % error
            else:
                report[phase] = "注册顺序(未全员声明 slot): %s" % ", ".join(
                    str(getattr(module, "slot", type(module).__name__))
                    for module in modules
                )
        return report

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
        async def install():
            async with self._reconcile_lock:
                return await self._install_files(source)

        return await CompositionRoot._physical(install())

    async def _install_files(self, source: str) -> str:
        from utils.async_tasks import run_sync_drained

        source = source.strip()
        if not source:
            return "Error: plugin source is empty"
        install_root = self.workspace / ".kirakira" / "plugins"
        install_root.mkdir(parents=True, exist_ok=True)
        staging = install_root / (".install-%s" % uuid4().hex)
        try:
            local = Path(source).expanduser()
            if local.is_dir():
                await run_sync_drained(shutil.copytree, local.resolve(), staging)
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
                    output, _ = await asyncio.wait_for(
                        process.communicate(), timeout=120
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    return "Error: plugin clone timed out"
                if process.returncode:
                    return (
                        "Error: plugin clone failed: %s"
                        % output.decode("utf-8", errors="replace")[-2000:]
                    )
            # 插件身份来自来源目录名：安装期不导入 plugin.py，绝不热执行刚下载的代码。
            if not (staging / "plugin.py").is_file():
                return "Error: plugin must contain plugin.py at its root"
            plugin_name = _source_plugin_name(source)
            if not self._valid_plugin_id(plugin_name):
                return "Error: invalid plugin name: %s" % plugin_name
            compile(
                (staging / "plugin.py").read_bytes(), str(staging / "plugin.py"), "exec"
            )
            target = install_root / plugin_name
            current = self._records.get(plugin_name)
            if current is not None and current.root != target:
                return "Error: plugin identity belongs to a bundled or separately managed installation"
            upgrading = target.exists()
            git_dir = staging / ".git"
            if git_dir.exists():
                await run_sync_drained(shutil.rmtree, git_dir)
            if upgrading:
                # Existing implementations may still read their resources.
                # Retain the directory until the exact owner physically settles.
                if self.root.activation("plugin:" + plugin_name) is not None:
                    await self.root.remove("plugin:" + plugin_name)
                    self._records.pop(plugin_name, None)
                # 升级:旧目录先挪走再原子换入,失败可回滚,不会留下半个插件。
                backup = install_root / (".backup-%s-%s" % (plugin_name, uuid4().hex))
                os.replace(target, backup)
                try:
                    os.replace(staging, target)
                except BaseException:
                    os.replace(backup, target)
                    raise
                await run_sync_drained(shutil.rmtree, backup, True)
            else:
                os.replace(staging, target)
            self._request_reload()
            return "%s plugin %r at %s. Hot reload will apply it shortly." % (
                "Upgraded" if upgrading else "Installed",
                plugin_name,
                target,
            )
        finally:
            if staging.exists():
                await run_sync_drained(shutil.rmtree, staging, True)
