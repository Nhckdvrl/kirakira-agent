from agent.composition.lifecycle import on_close, on_stopping

"""Source owners are independent of model, tool and conversation implementations."""

from plugins.ledger.contract import SESSIONS

from .admission import TurnAdmission
from .contract import ADMISSION, SOURCES
from .service import SourceSessions

inject = (SESSIONS,)
provides = (SOURCES, ADMISSION)


async def apply(ctx):
    sessions = ctx.get(SESSIONS)
    sources, admission = SourceSessions(sessions), TurnAdmission(sessions)
    await on_stopping(ctx, admission.close)
    await on_close(ctx, lambda: admission.close())
    await ctx.provide(SOURCES, sources)
    await ctx.provide(ADMISSION, admission)
