"""Declare this owner's dependencies, capabilities and resource lifetime."""

from agent.config import config_value
from plugins.context.context_builder import ContextBuilder
from plugins.context.contract import CONTEXT
from plugins.default_memory.contract import MEMORY

inject = (MEMORY,)
provides = (CONTEXT,)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    app_config = ctx.config["app_config"]
    memory = ctx.get(MEMORY)
    context = ContextBuilder(
        workdir,
        memory,
        system_prompt=str(
            config_value(app_config, "agent", "system_prompt", default="")
        ),
    )

    await ctx.provide(CONTEXT, context)
