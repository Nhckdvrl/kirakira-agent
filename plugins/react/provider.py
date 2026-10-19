"""Declare this owner's dependencies, capabilities and resource lifetime."""

import os

from agent.config import config_value
from bus.contract import EVENTS
from plugins.context.contract import CONTEXT
from plugins.default_memory.contract import RECALL_INSPECTOR
from plugins.execution.contract import EXECUTIONS
from plugins.extensions.contract import EXTENSIONS
from plugins.models.context_policy import recommended_context_settings
from plugins.models.contract import MODEL, MODEL_ID
from plugins.models.retry import ModelRetryPolicy
from plugins.react.contract import REACT, REACT_CONFIG, ReactConfig
from plugins.react.reasoner import ReactProgram
from plugins.tools.contract import TOOLS
from utils.environment import env_bool

inject = (
    MODEL,
    MODEL_ID,
    CONTEXT,
    EVENTS,
    TOOLS,
    EXECUTIONS,
    EXTENSIONS,
    RECALL_INSPECTOR,
)
provides = (REACT_CONFIG, REACT)


async def apply(ctx):
    app_config = ctx.config["app_config"]
    client = ctx.get(MODEL)
    model = ctx.get(MODEL_ID)
    context = ctx.get(CONTEXT)
    event_bus = ctx.get(EVENTS)
    registry = ctx.get(TOOLS)
    execution_store = ctx.get(EXECUTIONS)
    plugin_manager = ctx.get(EXTENSIONS)
    recall_inspector = ctx.get(RECALL_INSPECTOR)
    # 未显式配置 memory_window / max_tokens 时，按模型真实 context_window 等比例派生。
    derived = recommended_context_settings(
        int(config_value(app_config, "llm", "main", "context_window", default=128_000)),
        float(
            config_value(
                app_config, "agent", "context", "effective_context_percent", default=0.9
            )
        ),
    )
    config = ReactConfig(
        model=model,
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
        max_iterations=int(
            os.getenv(
                "AGENT_MAX_ITERATIONS",
                str(config_value(app_config, "agent", "max_iterations", default=10)),
            )
        ),
        max_tokens=int(
            os.getenv(
                "AGENT_MAX_TOKENS",
                str(
                    config_value(
                        app_config,
                        "agent",
                        "max_tokens",
                        default=derived.output_reserve,
                    )
                ),
            )
        ),
        history_window=int(
            os.getenv(
                "AGENT_HISTORY_WINDOW",
                str(
                    config_value(
                        app_config,
                        "agent",
                        "context",
                        "memory_window",
                        default=derived.memory_window,
                    )
                ),
            )
        ),
        model_timeout_seconds=float(os.getenv("AGENT_MODEL_TIMEOUT", "120")),
        repeated_tool_call_limit=int(os.getenv("AGENT_REPEATED_TOOL_LIMIT", "3")),
        max_parallel_tool_calls=max(1, int(os.getenv("AGENT_MAX_PARALLEL_TOOLS", "4"))),
        stream=env_bool("AGENT_STREAM", True),
        retry_policy=ModelRetryPolicy(
            max_retries=int(
                config_value(app_config, "llm", "main", "max_retries", default=10)
            )
        ),
    )
    reasoner = ReactProgram(
        model_client=client,
        tools=registry,
        config=config,
        context=context,
        event_bus=event_bus,
        execution_store=execution_store,
    )
    reasoner._tool_io._tool_executor._hooks.provider = lambda: plugin_manager.tool_hooks
    reasoner._prompt_render_modules.provider = lambda: (
        plugin_manager.prompt_render_modules
    )
    reasoner._before_step_modules.provider = lambda: plugin_manager.before_step_modules
    reasoner._after_step_modules.provider = lambda: plugin_manager.after_step_modules

    reasoner.add_tool_hooks([recall_inspector.tool_hook()])

    await ctx.provide(REACT_CONFIG, config)
    await ctx.provide(REACT, reasoner)
