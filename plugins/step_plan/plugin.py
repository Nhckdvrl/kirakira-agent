"""Subscription-only Step5 driver; no alternate billing endpoint."""
from plugins.models.contract import MODEL_DRIVERS

from .driver import definition

api_version = 3
name = "step-plan"
version = "1.0.0"
desc = "Step5 via Step Token Plan"
inject = (MODEL_DRIVERS,)


async def apply(ctx):
    await ctx.require(MODEL_DRIVERS).register(ctx, definition())
