from agent.composition.lifecycle import on_close
from plugins.ledger.contract import SESSIONS

from .contract import JOBS
from .service import JobNotices

inject = (SESSIONS,)
provides = (JOBS,)


async def apply(ctx):
    jobs = JobNotices(ctx.get(SESSIONS))
    await on_close(ctx, jobs.close)
    await ctx.provide(JOBS, jobs)
