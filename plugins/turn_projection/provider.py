"""The projection owns no storage, cursor or running task."""

from .contract import TURN_PROJECTION
from .plugin import TurnProjection

inject = ()
provides = (TURN_PROJECTION,)


async def apply(ctx):
    await ctx.provide(TURN_PROJECTION, TurnProjection())
