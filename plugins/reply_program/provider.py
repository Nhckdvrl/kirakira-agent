"""Durable reply programs use the ledger and source owner, independent of ingress."""

from plugins.ledger.contract import INPUTS, MESSAGES, SESSIONS
from plugins.sources.contract import SOURCES

from .contract import PROGRAMS
from .program import ReplyPrograms

inject = (SESSIONS, SOURCES, INPUTS, MESSAGES)
provides = (PROGRAMS,)


async def apply(ctx):
    await ctx.provide(
        PROGRAMS,
        ReplyPrograms(
            ctx.get(SESSIONS), ctx.get(SOURCES), ctx.get(INPUTS), ctx.get(MESSAGES)
        ),
    )
