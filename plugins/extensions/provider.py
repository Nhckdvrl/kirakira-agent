from agent.composition.lifecycle import on_close, on_start, on_stopping

"""Application composition root. Channel and proactive wiring have independent owners."""

from pathlib import Path

from bus.contract import EVENTS
from plugins.context.contract import CONTEXT
from plugins.extensions import PluginManager
from plugins.extensions.contract import EXTENSION_WATCHER, EXTENSIONS
from plugins.extensions.watcher import PluginWatcher
from plugins.mcp.contract import MCP_PUBLISHER
from plugins.tools.contract import TOOLS

inject = (EVENTS, TOOLS, MCP_PUBLISHER, CONTEXT)
provides = (EXTENSIONS, EXTENSION_WATCHER)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    ctx.config["app_config"]
    event_bus = ctx.get(EVENTS)
    registry = ctx.get(TOOLS)
    mcp_publisher = ctx.get(MCP_PUBLISHER)
    context = ctx.get(CONTEXT)
    bundled_plugin_root = (
        Path(__file__).resolve().parents[2] / "plugin_packages" / "curated_feeds"
    )
    plugin_roots = [workdir / ".kirakira" / "plugins"]
    if bundled_plugin_root.is_dir():
        plugin_roots.append(bundled_plugin_root)
    plugin_manager = PluginManager(
        plugin_roots,
        event_bus=event_bus,
        tool_registry=registry,
        workspace=workdir,
        mcp_publisher=mcp_publisher,
        skill_loader=context.skills,
        service_registry=ctx.config["_host"].services,
        composition=ctx.config["_host"],
        activation_context=ctx,
    )
    await on_close(ctx, plugin_manager._withdraw_management_tools)
    await on_close(ctx, lambda: plugin_manager.terminate_all())
    await plugin_manager.load_all()
    plugin_watcher = PluginWatcher(plugin_manager)
    # 安装/卸载/启停后立即热重载,不必重启进程。
    plugin_manager.reload_hook = plugin_watcher.wake

    await ctx.provide(EXTENSIONS, plugin_manager)
    await ctx.provide(EXTENSION_WATCHER, plugin_watcher)

    await on_start(ctx, lambda: ctx.spawn(plugin_watcher.run(), name="plugin_watcher"))
    await on_stopping(ctx, plugin_watcher.stop)
