from agent.composition.lifecycle import on_close, on_start

"""Declare this owner's dependencies, capabilities and resource lifetime."""

import os

from agent.config import config_value
from bus.contract import EVENTS
from core.memory.legacy import MemoryRuntime
from infra.providers.model_client_adapter import ModelClientProvider
from plugins.default_memory.contract import (
    MEMORY,
    MEMORY_READER,
    MEMORY_SERVICES,
    RECALL_INSPECTOR,
    MemoryReader,
)
from plugins.default_memory.factory import build_memory_services
from plugins.default_memory.inspector import RecallInspector
from plugins.ledger.contract import SESSIONS
from plugins.models.contract import MEMORY_MODEL

inject = (MEMORY_MODEL, EVENTS, SESSIONS)
provides = (MEMORY_SERVICES, MEMORY_READER, MEMORY, RECALL_INSPECTOR)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    app_config = ctx.config["app_config"]
    memory_client = ctx.get(MEMORY_MODEL)
    event_bus = ctx.get(EVENTS)
    session_manager = ctx.get(SESSIONS)
    memory_provider = ModelClientProvider(memory_client)
    memory_services = build_memory_services(
        app_config=app_config,
        workspace=workdir,
        provider=memory_provider,
        light_provider=memory_provider,
        event_publisher=event_bus,
        session_manager=session_manager,
        memory_window=int(
            config_value(app_config, "agent", "context", "memory_window", default=40)
        ),
    )
    await on_close(ctx, lambda: memory_services.aclose())
    memory = MemoryRuntime(
        workdir,
        session_manager=session_manager,
        engine=str(config_value(app_config, "memory", "engine", default="auto")),
        shared_store=memory_services.store,
        event_bus=event_bus,
    )
    await on_close(ctx, lambda: memory.shutdown())
    embedding_model = os.getenv("EMBEDDING_MODEL_ID") or str(
        config_value(app_config, "memory", "embedding", "model", default="")
    )
    embedding_base_url = os.getenv("EMBEDDING_BASE_URL") or str(
        config_value(app_config, "memory", "embedding", "base_url", default="")
    )
    if embedding_model and embedding_base_url:
        memory.configure_embeddings(
            model=embedding_model,
            base_url=embedding_base_url,
            api_key=os.getenv("EMBEDDING_API_KEY")
            or str(
                config_value(app_config, "memory", "embedding", "api_key", default="")
            ),
        )

    recall_inspector = RecallInspector(workdir)

    await ctx.provide(MEMORY_SERVICES, memory_services)
    await ctx.provide(MEMORY_READER, MemoryReader(memory_services.engine))
    await ctx.provide(MEMORY, memory)
    await ctx.provide(RECALL_INSPECTOR, recall_inspector)

    await on_start(
        ctx,
        lambda: ctx.spawn(memory_services.run_maintenance(), name="memory_maintenance"),
    )
