"""Model Context Protocol stdio client and declarative workspace servers."""

from plugins.mcp.admin import WorkspaceMcpAdmin
from plugins.mcp.client import McpClient, McpToolInfo
from plugins.mcp.declarations import (
    WorkspaceMcpDeclarations,
    declarations_input_revision,
    load_workspace_mcp_declarations,
)
from plugins.mcp.host import McpGenerationHost, PreparedMcpCatalog
from plugins.mcp.publisher import McpCatalogPublisher
from plugins.mcp.watcher import WorkspaceMcpWatcher

__all__ = [
    "McpCatalogPublisher",
    "McpClient",
    "McpGenerationHost",
    "McpToolInfo",
    "PreparedMcpCatalog",
    "WorkspaceMcpAdmin",
    "WorkspaceMcpDeclarations",
    "WorkspaceMcpWatcher",
    "declarations_input_revision",
    "load_workspace_mcp_declarations",
]
