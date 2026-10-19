from agent.composition.lifecycle import on_close

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from bus.contract import BUS, EVENTS
from plugins.default_memory.contract import MEMORY, MEMORY_SERVICES, RECALL_INSPECTOR
from plugins.delivery.contract import DELIVERY
from plugins.extensions.contract import EXTENSIONS
from plugins.ledger.contract import SESSIONS
from plugins.jobs.contract import JOBS
from plugins.react.contract import REACT, REACT_CONFIG
from plugins.reply_program.contract import DEFAULT_REPLY, PROGRAMS
from plugins.reply_program.service import DefaultReplyProgram
from plugins.tools.contract import TOOLS

inject = (
    BUS,
    EVENTS,
    SESSIONS,
    MEMORY,
    TOOLS,
    REACT,
    REACT_CONFIG,
    MEMORY_SERVICES,
    RECALL_INSPECTOR,
    EXTENSIONS,
    PROGRAMS,
    DELIVERY,
    JOBS,
)
provides = (DEFAULT_REPLY,)


async def apply(ctx):
    bus = ctx.get(BUS)
    event_bus = ctx.get(EVENTS)
    session_manager = ctx.get(SESSIONS)
    memory = ctx.get(MEMORY)
    registry = ctx.get(TOOLS)
    reasoner = ctx.get(REACT)
    config = ctx.get(REACT_CONFIG)
    memory_services = ctx.get(MEMORY_SERVICES)
    recall_inspector = ctx.get(RECALL_INSPECTOR)
    plugin_manager = ctx.get(EXTENSIONS)
    pipeline = DefaultReplyProgram(
        delivery=ctx.get(DELIVERY),
        bus=bus,
        event_bus=event_bus,
        session_manager=session_manager,
        memory=memory,
        tools=registry,
        reasoner=reasoner,
        config=config,
        memory_services=memory_services,
        recall_inspector=recall_inspector,
        programs=ctx.get(PROGRAMS),
        jobs=ctx.get(JOBS),
    )
    pipeline._before_turn_modules.provider = lambda: plugin_manager.before_turn_modules
    pipeline._before_reasoning_modules.provider = lambda: (
        plugin_manager.before_reasoning_modules
    )
    pipeline._after_reasoning_modules.provider = lambda: (
        plugin_manager.after_reasoning_modules
    )
    pipeline._after_turn_modules.provider = lambda: plugin_manager.after_turn_modules

    await on_close(ctx, lambda: pipeline.close())

    await ctx.provide(DEFAULT_REPLY, pipeline)
