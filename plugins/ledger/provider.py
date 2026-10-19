from agent.composition.lifecycle import on_close

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from plugins.ledger.contract import INPUTS, SESSIONS
from plugins.ledger.inputs import InputLedger
from plugins.ledger.manager import SessionManager

inject = ()
provides = (SESSIONS, INPUTS)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    session_manager = SessionManager(workdir)
    await on_close(ctx, lambda: session_manager.close())
    session_manager.recover_admissions()

    await ctx.provide(SESSIONS, session_manager)
    await ctx.provide(INPUTS, InputLedger(session_manager))
