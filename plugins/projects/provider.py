"""Declare this owner's dependencies, capabilities and resource lifetime."""

from plugins.ledger.contract import SESSIONS
from plugins.projects.contract import WORKSPACE

inject = (SESSIONS,)
provides = (WORKSPACE,)


async def apply(ctx):
    session_manager = ctx.get(SESSIONS)
    from plugins.projects.factory import build_workspace_services

    workspace_services = build_workspace_services(session_manager)

    await ctx.provide(WORKSPACE, workspace_services)
