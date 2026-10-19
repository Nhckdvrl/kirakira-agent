"""Compose workspace and memory tool providers for a conversation runtime."""

from pathlib import Path
from typing import Any

from bus.queue import MessageBus
from core.memory.legacy import MemoryRuntime
from plugins.ledger.manager import SessionManager
from plugins.memory_tools.tools import register_memory_tools
from plugins.tools import build_workspace_registry
from plugins.tools.registry import ToolRegistry


def build_tool_registry(
    workdir: Path,
    *,
    memory: MemoryRuntime | None = None,
    session_manager: SessionManager | None = None,
    bus: MessageBus | None = None,
    push_tool: Any = None,
    memory_services: Any = None,
    workspace_services: Any = None,
) -> ToolRegistry:
    registry = build_workspace_registry(workdir, bus=bus, push_tool=push_tool)
    if workspace_services is not None:
        registry.directory_provider = workspace_services.directories
        workspace_services.directories.register_tools(registry)
    register_memory_tools(
        registry,
        memory=memory,
        session_manager=session_manager,
        memory_services=memory_services,
    )
    return registry
