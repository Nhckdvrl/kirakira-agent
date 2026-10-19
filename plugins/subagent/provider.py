from agent.composition.lifecycle import on_close, on_stopping

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from plugins.default_memory.contract import MEMORY
from plugins.ledger.contract import SESSIONS
from plugins.jobs.contract import JOBS
from plugins.react.contract import REACT
from plugins.subagent.contract import SUBAGENTS
from plugins.subagent.service import SubagentManager
from plugins.tools.contract import TOOLS
from plugins.tools.registry import ToolRegistry

inject = (REACT, TOOLS, SESSIONS, MEMORY, JOBS)
provides = (SUBAGENTS,)


async def apply(ctx):
    reasoner = ctx.get(REACT)
    registry = ctx.get(TOOLS)
    session_manager = ctx.get(SESSIONS)
    memory = ctx.get(MEMORY)
    subagents = SubagentManager(
        reasoner=reasoner,
        tools=registry,
        sessions=session_manager,
        memory=memory,
        jobs=ctx.get(JOBS),
    )
    await on_close(ctx, lambda: subagents.shutdown())
    staging = ToolRegistry()
    subagents.register_tool(staging)
    await registry.install_owned(ctx, staging)

    await ctx.provide(SUBAGENTS, subagents)

    await on_stopping(ctx, subagents.shutdown)
