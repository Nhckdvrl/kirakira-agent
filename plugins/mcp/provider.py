from agent.composition.lifecycle import on_close, on_start, on_stopping

"""Declare this owner's dependencies, capabilities and resource lifetime."""

import logging

from plugins.mcp import McpCatalogPublisher, WorkspaceMcpAdmin, WorkspaceMcpWatcher
from plugins.mcp.contract import MCP_PUBLISHER, MCP_WATCHER
from plugins.tools.contract import TOOLS

inject = (TOOLS,)
provides = (MCP_PUBLISHER, MCP_WATCHER)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    registry = ctx.get(TOOLS)
    mcp_publisher = McpCatalogPublisher(
        registry, composition=ctx.config["_host"], parent=ctx
    )
    # workspace MCP 由 mcp/servers/*.toml 声明并热重载；首轮 reconcile 失败不阻塞启动，
    # watcher 会在声明修好后自动重试。
    mcp_watcher = WorkspaceMcpWatcher(workdir / "mcp" / "servers", mcp_publisher)
    await on_close(ctx, lambda: mcp_watcher.shutdown())
    # 让 agent 也能自己增删声明，走的仍是 watcher 那条 reconcile 路径。
    WorkspaceMcpAdmin(workdir, mcp_watcher).register_tools(registry)
    try:
        await mcp_watcher.reconcile()
    except Exception as error:
        mcp_watcher.last_error = str(error)
        logging.getLogger(__name__).error(
            "workspace MCP initial publish failed: %s", error
        )

    await ctx.provide(MCP_PUBLISHER, mcp_publisher)
    await ctx.provide(MCP_WATCHER, mcp_watcher)

    await on_start(
        ctx, lambda: ctx.spawn(mcp_watcher.run(), name="workspace_mcp_watcher")
    )
    await on_stopping(ctx, mcp_watcher.stop)
