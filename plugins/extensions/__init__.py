"""Canonical plugin runtime API."""

from agent.composition import ServiceKey, ServiceUnavailable
from plugins.extensions.api import Plugin, PluginContext, PluginKVStore
from plugins.extensions.decorators import (
    on_after_reasoning,
    on_after_step,
    on_after_turn,
    on_before_reasoning,
    on_before_step,
    on_before_turn,
    on_prompt_render,
    on_tool_pre,
    tool,
)
from plugins.extensions.manager import PluginManager
from plugins.extensions.specs import (
    ManagedServiceSpec,
    McpServerSpec,
    PluginReadinessContext,
    PluginSemanticCheck,
    ProactiveSourceSpec,
)

__all__ = [
    "ServiceKey",
    "ServiceUnavailable",
    "ManagedServiceSpec",
    "McpServerSpec",
    "Plugin",
    "PluginContext",
    "PluginKVStore",
    "PluginManager",
    "PluginReadinessContext",
    "PluginSemanticCheck",
    "ProactiveSourceSpec",
    "on_after_reasoning",
    "on_after_step",
    "on_after_turn",
    "on_before_reasoning",
    "on_before_step",
    "on_before_turn",
    "on_prompt_render",
    "on_tool_pre",
    "tool",
]
