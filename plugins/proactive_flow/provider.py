from agent.composition.lifecycle import on_close, on_start, on_stopping

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from bus.contract import BUS
from plugins.default_memory.contract import MEMORY, MEMORY_SERVICES
from plugins.execution.contract import EXECUTIONS
from plugins.extensions.contract import EXTENSIONS
from plugins.ledger.contract import SESSIONS
from plugins.models.contract import MODEL, MODEL_ID
from plugins.proactive_flow.contract import DRIFT, PROACTIVE
from plugins.proactive_flow.factory import build_proactive
from plugins.reply.contract import FOLLOWER
from plugins.tools.contract import TOOLS

inject = (
    BUS,
    SESSIONS,
    MEMORY,
    MODEL,
    MODEL_ID,
    FOLLOWER,
    MEMORY_SERVICES,
    EXTENSIONS,
    TOOLS,
    EXECUTIONS,
)
provides = (PROACTIVE, DRIFT)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    app_config = ctx.config["app_config"]
    bus = ctx.get(BUS)
    session_manager = ctx.get(SESSIONS)
    memory = ctx.get(MEMORY)
    client = ctx.get(MODEL)
    model = ctx.get(MODEL_ID)
    loop = ctx.get(FOLLOWER)
    memory_services = ctx.get(MEMORY_SERVICES)
    plugin_manager = ctx.get(EXTENSIONS)
    registry = ctx.get(TOOLS)
    execution_store = ctx.get(EXECUTIONS)
    proactive_loop, drift_runner = build_proactive(
        workdir=workdir,
        app_config=app_config,
        default_model=model,
        bus=bus,
        session_manager=session_manager,
        memory=memory,
        client=client,
        passive_busy_fn=loop.is_busy,
        memory_services=memory_services,
        plugin_sources=plugin_manager.proactive_sources,
        tool_registry=registry,
        # tick 与被动 turn 同一对租约保证:换代不抽走在途能力。
        drift_skill_roots_provider=lambda: plugin_manager.drift_skill_roots,
        execution_store=execution_store,
    )
    if proactive_loop is not None:
        await on_close(ctx, lambda: proactive_loop.close())
    if drift_runner is not None:
        await on_close(ctx, lambda: drift_runner.close())

    await ctx.provide(PROACTIVE, proactive_loop)
    await ctx.provide(DRIFT, drift_runner)

    if proactive_loop is not None:
        await on_start(
            ctx, lambda: ctx.spawn(proactive_loop.run(), name="proactive_loop")
        )
        await on_stopping(ctx, proactive_loop.stop)
