"""Application launch gate; ownership stays in native Context/Effect."""

from agent.plugin_composition import RUNTIME_STOPPING


async def on_close(ctx, closer):
    return await ctx.effect(lambda: closer, label="resource")


async def on_stopping(ctx, action):
    async def stopping(_):
        await _invoke(action)

    return await ctx.on(RUNTIME_STOPPING, stopping)


async def on_start(ctx, action):
    return await ctx.config["_host"].register_start(ctx, action)


async def on_ready(ctx, action):
    return await ctx.config["_host"].register_start(ctx, action, ready=True)


async def _invoke(action):
    import inspect

    value = action()
    if inspect.isawaitable(value):
        await value
