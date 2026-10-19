"""Declare this owner's dependencies, capabilities and resource lifetime."""

import asyncio

from plugins.control.contract import CONTROL
from plugins.dashboard.contract import DASHBOARD
from plugins.dashboard.service import DashboardService
from plugins.default_memory.contract import MEMORY, MEMORY_SERVICES, RECALL_INSPECTOR
from plugins.extensions.contract import EXTENSIONS
from plugins.ledger.contract import SESSIONS
from plugins.proactive_flow.contract import DRIFT, PROACTIVE
from plugins.runtime_restart.contract import RESTART

inject = (
    SESSIONS,
    MEMORY_SERVICES,
    MEMORY,
    EXTENSIONS,
    RECALL_INSPECTOR,
    PROACTIVE,
    DRIFT,
    RESTART,
    CONTROL,
)
provides = (DASHBOARD,)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    session_manager = ctx.get(SESSIONS)
    memory_services = ctx.get(MEMORY_SERVICES)
    memory = ctx.get(MEMORY)
    plugin_manager = ctx.get(EXTENSIONS)
    recall_inspector = ctx.get(RECALL_INSPECTOR)
    proactive_loop = ctx.get(PROACTIVE)
    drift_runner = ctx.get(DRIFT)
    restart = ctx.get(RESTART)
    control = ctx.get(CONTROL)
    dashboard = DashboardService(
        workspace=workdir,
        session_manager=session_manager,
        memory_services=memory_services,
        memory=memory,
        plugin_manager=plugin_manager,
        restart_coordinator=restart[1],
        recall_inspector=recall_inspector,
        # 状态库连接归本循环所在线程独占;Web 的 HTTP handler 在别的线程,读要 marshal 回来。
        loop=asyncio.get_running_loop(),
    )

    dashboard.proactive_loop = proactive_loop
    dashboard.drift_runner = drift_runner
    dashboard.extra["workspace_control"] = control[2]

    await ctx.provide(DASHBOARD, dashboard)
