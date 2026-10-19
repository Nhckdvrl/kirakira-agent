from agent.composition.lifecycle import on_close, on_start, on_stopping

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from bus.contract import BUS
from plugins.conversation.contract import CONVERSATION
from plugins.execution.contract import EXECUTIONS
from plugins.ledger.contract import SESSIONS
from plugins.scheduler.contract import SCHEDULER
from plugins.scheduler.service import SchedulerService
from plugins.tools.contract import TOOLS

inject = (BUS, TOOLS, CONVERSATION, SESSIONS, EXECUTIONS)
provides = (SCHEDULER,)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    bus = ctx.get(BUS)
    registry = ctx.get(TOOLS)
    pipeline = ctx.get(CONVERSATION)
    session_manager = ctx.get(SESSIONS)
    execution_store = ctx.get(EXECUTIONS)
    scheduler = SchedulerService(
        workdir / ".kirakira" / "schedules.json",
        bus=bus,
        tools=registry,
        pipeline=pipeline,
        sessions=session_manager,
        executions=execution_store,
    )
    await on_close(ctx, lambda: scheduler.shutdown())

    await ctx.provide(SCHEDULER, scheduler)

    await on_start(ctx, lambda: ctx.spawn(scheduler.run(), name="scheduler"))
    await on_stopping(ctx, scheduler.shutdown)
