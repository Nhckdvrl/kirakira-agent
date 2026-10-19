from agent.composition.lifecycle import on_close

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from plugins.execution.contract import EXECUTIONS
from plugins.execution.service import ExecutionStore

inject = ()
provides = (EXECUTIONS,)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    execution_store = ExecutionStore(workdir / ".kirakira" / "executions.db")
    await on_close(ctx, lambda: execution_store.close())
    execution_store.recover()

    await ctx.provide(EXECUTIONS, execution_store)
