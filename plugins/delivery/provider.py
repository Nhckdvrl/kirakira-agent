"""The delivery owner holds message commit and post-commit publication."""

from bus.contract import BUS, EVENTS
from plugins.ledger.contract import SESSIONS
from plugins.reply_program.contract import PROGRAMS

from .contract import DELIVERY
from .service import DeliveryService

inject = (BUS, EVENTS, SESSIONS, PROGRAMS)
provides = (DELIVERY,)


async def apply(ctx):
    await ctx.provide(
        DELIVERY,
        DeliveryService(
            ctx.get(SESSIONS), ctx.get(BUS), ctx.get(EVENTS), ctx.get(PROGRAMS)
        ),
    )
