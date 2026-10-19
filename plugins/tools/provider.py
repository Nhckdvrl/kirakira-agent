from agent.composition.lifecycle import on_close

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from bus.contract import BUS
from plugins.default_memory.contract import MEMORY, MEMORY_SERVICES
from plugins.ledger.contract import SESSIONS
from plugins.projects.contract import WORKSPACE
from plugins.tools.contract import PUSH, TOOLS
from plugins.tools.factory import build_tool_registry

inject = (BUS, MEMORY, MEMORY_SERVICES, SESSIONS, WORKSPACE)
provides = (TOOLS, PUSH)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    bus = ctx.get(BUS)
    memory = ctx.get(MEMORY)
    memory_services = ctx.get(MEMORY_SERVICES)
    session_manager = ctx.get(SESSIONS)
    workspace_services = ctx.get(WORKSPACE)
    from plugins.tools.message_push import MessagePushTool

    push_tool = MessagePushTool(chat_lane=bus.chat_lane)
    registry = build_tool_registry(
        workdir,
        memory=memory,
        session_manager=session_manager,
        bus=bus,
        push_tool=push_tool,
        memory_services=memory_services,
        workspace_services=workspace_services,
    )
    await on_close(ctx, lambda: registry.shutdown())

    await ctx.provide(TOOLS, registry)
    await ctx.provide(PUSH, push_tool)
