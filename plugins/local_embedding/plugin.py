"""Local CPU embeddings are an ordinary Models driver, not a second memory store."""

from plugins.models.contract import MODEL_DRIVERS

from .driver import definition

api_version = 3
name = "local-embedding"
version = "1.0.0"
desc = "Pinned offline ONNX embeddings for Akasha and semantic interest"
inject = (MODEL_DRIVERS,)


async def apply(ctx):
    await ctx.require(MODEL_DRIVERS).register(ctx, definition())
