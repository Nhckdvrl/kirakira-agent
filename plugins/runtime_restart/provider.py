"""Declare this owner's dependencies, capabilities and resource lifetime."""

from plugins.runtime_restart.contract import RESTART
from plugins.runtime_restart.coordination import (
    RestartCoordinator,
    SupervisorCommitChannel,
)
from plugins.runtime_restart.tools import register_agent_restart_tool
from plugins.tools.contract import TOOLS

inject = (TOOLS,)
provides = (RESTART,)


async def apply(ctx):
    registry = ctx.get(TOOLS)
    restart_coordinator = None
    commit_channel = SupervisorCommitChannel.from_environment()
    if commit_channel is not None:
        restart_coordinator = RestartCoordinator(
            commit_channel.boot_id,
            supervised=True,
            commit=commit_channel.commit,
        )
    if restart_coordinator is not None:
        register_agent_restart_tool(registry, restart_coordinator)

    await ctx.provide(RESTART, (commit_channel, restart_coordinator))
