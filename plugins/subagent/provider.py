from agent.composition.lifecycle import on_close, on_stopping

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from bus.contract import BUS
from plugins.default_memory.contract import MEMORY
from plugins.ledger.contract import SESSIONS
from plugins.react.contract import REACT
from plugins.subagent.contract import SUBAGENTS
from plugins.subagent.service import SubagentManager
from plugins.tools.contract import TOOLS

inject = (REACT, TOOLS, SESSIONS, MEMORY, BUS)
provides = (SUBAGENTS,)


async def apply(ctx):
    reasoner = ctx.get(REACT)
    registry = ctx.get(TOOLS)
    session_manager = ctx.get(SESSIONS)
    memory = ctx.get(MEMORY)
    bus = ctx.get(BUS)
    subagents = SubagentManager(
        reasoner=reasoner,
        tools=registry,
        sessions=session_manager,
        memory=memory,
        bus=bus,
    )
    await on_close(ctx, lambda: subagents.shutdown())
    subagents.register_tool()

    await ctx.provide(SUBAGENTS, subagents)

    await on_stopping(ctx, subagents.shutdown)
