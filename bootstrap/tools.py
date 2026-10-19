"""Compose workspace and memory tool providers for a conversation runtime."""

from pathlib import Path
from typing import Any

from agent.tools import build_workspace_registry
from agent.tools.registry import ToolRegistry
from bus.queue import MessageBus
from core.memory.legacy import MemoryRuntime
from plugins.memory_tools.tools import register_memory_tools
from session.manager import SessionManager


def build_tool_registry(
    workdir: Path,
    *,
    memory: MemoryRuntime | None = None,
    session_manager: SessionManager | None = None,
    bus: MessageBus | None = None,
    push_tool: Any = None,
    memory_services: Any = None,
) -> ToolRegistry:
    registry = build_workspace_registry(workdir, bus=bus, push_tool=push_tool)
    register_memory_tools(
        registry,
        memory=memory,
        session_manager=session_manager,
        memory_services=memory_services,
    )
    return registry
