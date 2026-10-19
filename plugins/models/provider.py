"""Declare this owner's dependencies, capabilities and resource lifetime."""

import os
from typing import Any

from agent.config import config_value, require_env
from infra.providers.step_plan import create_model_client
from plugins.models.contract import MEMORY_MODEL, MODEL, MODEL_ID

inject = ()
provides = (MODEL, MEMORY_MODEL, MODEL_ID)


async def apply(ctx):
    app_config = ctx.config["app_config"]
    model = os.getenv("MODEL_ID") or str(
        config_value(app_config, "llm", "main", "model", default="")
    )
    if not model:
        model = require_env("MODEL_ID")
    client = create_model_client(
        reasoning_effort=config_value(
            app_config, "llm", "main", "reasoning_effort", default="medium"
        ),
        base_url=os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        or config_value(app_config, "llm", "main", "base_url"),
        api_key=os.getenv("OPENAI_COMPATIBLE_API_KEY")
        or config_value(app_config, "llm", "main", "api_key", default=""),
        thinking_enabled=config_value(app_config, "llm", "main", "enable_thinking"),
        context_window=int(
            config_value(app_config, "llm", "main", "context_window", default=0)
        ),
        effective_context_percent=float(
            config_value(
                app_config,
                "agent",
                "context",
                "effective_context_percent",
                default=0.9,
            )
        ),
    )
    memory_client: Any = client
    light_model = os.getenv("LIGHT_MODEL_ID") or str(
        config_value(app_config, "llm", "light", "model", default="") or ""
    )
    light_base_url = os.getenv("LIGHT_OPENAI_COMPATIBLE_BASE_URL") or str(
        config_value(app_config, "llm", "light", "base_url", default="") or ""
    )
    if light_model and light_base_url:
        from plugins.models.fallback import ResilientModelClient

        light_client = create_model_client(
            reasoning_effort=config_value(
                app_config, "llm", "light", "reasoning_effort", default="medium"
            ),
            base_url=light_base_url,
            api_key=os.getenv("LIGHT_OPENAI_COMPATIBLE_API_KEY")
            or config_value(app_config, "llm", "light", "api_key", default="")
            or os.getenv("OPENAI_COMPATIBLE_API_KEY")
            or config_value(app_config, "llm", "main", "api_key", default=""),
            thinking_enabled=config_value(
                app_config, "llm", "light", "enable_thinking", default=False
            ),
            context_window=int(
                config_value(
                    app_config,
                    "llm",
                    "light",
                    "context_window",
                    default=config_value(
                        app_config, "llm", "main", "context_window", default=0
                    ),
                )
            ),
            effective_context_percent=float(
                config_value(
                    app_config,
                    "agent",
                    "context",
                    "effective_context_percent",
                    default=0.9,
                )
            ),
        )
        memory_client = ResilientModelClient(
            primary=light_client,
            primary_runtime_id="light",
            primary_model=light_model,
            fallback=client,
            fallback_model=model,
        )

    await ctx.provide(MODEL, client)
    await ctx.provide(MEMORY_MODEL, memory_client)
    await ctx.provide(MODEL_ID, model)
