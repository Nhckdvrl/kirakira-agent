"""Model Context Protocol stdio client and declarative workspace servers."""

from kirakira_agent.mcp.admin import WorkspaceMcpAdmin
from kirakira_agent.mcp.client import McpClient, McpToolInfo
from kirakira_agent.mcp.declarations import (
    WorkspaceMcpDeclarations,
    declarations_input_revision,
    load_workspace_mcp_declarations,
)
from kirakira_agent.mcp.host import McpGenerationHost, PreparedMcpCatalog
from kirakira_agent.mcp.publisher import McpCatalogPublisher
from kirakira_agent.mcp.watcher import WorkspaceMcpWatcher

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
