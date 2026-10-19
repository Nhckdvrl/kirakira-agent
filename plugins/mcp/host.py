"""Own physical MCP catalogs, including partially connected failed setups."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

from plugins.mcp.client import McpClient, McpToolInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedMcpServer:
    name: str
    client: McpClient
    tools: Tuple[McpToolInfo, ...]

    @property
    def remote_tool_names(self) -> Tuple[str, ...]:
        return tuple(info.name for info in self.tools)


@dataclass(frozen=True)
class PreparedMcpCatalog:
    generation_id: str
    servers: Mapping[str, PreparedMcpServer]

    @property
    def tool_names(self) -> Tuple[str, ...]:
        return tuple(
            sorted(
                info.name for server in self.servers.values() for info in server.tools
            )
        )


class McpGenerationHost:
    """准备并按代际持有 MCP catalog。"""

    def __init__(self) -> None:
        self._catalogs: Dict[str, PreparedMcpCatalog] = {}

    async def prepare(
        self,
        generation_id: str,
        *,
        server_specs: Mapping[str, Mapping[str, Any]],
        required_tools: Mapping[str, Tuple[str, ...]] | None = None,
    ) -> PreparedMcpCatalog:
        """连接全部候选 server，并在完整校验后登记 catalog。"""

        # 1. 连接候选 server；任一失败都要清理本批已连接的客户端。
        servers: Dict[str, PreparedMcpServer] = {}
        self._catalogs[generation_id] = PreparedMcpCatalog(
            generation_id, MappingProxyType(servers)
        )
        try:
            for server_name, spec in sorted(server_specs.items()):
                client = McpClient(
                    "%s@%s" % (server_name, generation_id),
                    list(spec["command"]),
                    env=dict(spec.get("env") or {}),
                    cwd=str(spec.get("cwd") or "") or None,
                )
                servers[server_name] = PreparedMcpServer(server_name, client, ())
                infos = await client.connect()
                remote_names = [info.name for info in infos]
                if len(remote_names) != len(set(remote_names)):
                    raise RuntimeError(
                        "duplicate tool names from MCP server: %s" % server_name
                    )
                servers[server_name] = PreparedMcpServer(
                    name=server_name,
                    client=client,
                    tools=tuple(infos),
                )

            # 2. 校验上层声明依赖的远端工具，再发布不可变 catalog。
            self._validate_required_tools(servers, required_tools or {})
        except BaseException as error:
            try:
                await self.close(generation_id)
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "MCP setup and cleanup failed", [error, cleanup_error]
                ) from None
            raise

        catalog = PreparedMcpCatalog(
            generation_id=generation_id,
            servers=MappingProxyType(dict(servers)),
        )
        self._catalogs[generation_id] = catalog
        return catalog

    async def close(self, generation_id: str) -> None:
        catalog = self._catalogs.get(generation_id)
        if catalog is None:
            return
        failures = await self._disconnect_all(catalog.servers.values())
        if failures:
            raise RuntimeError(
                "MCP catalog cleanup failed: "
                + "; ".join(str(error) for error in failures)
            )
        self._catalogs.pop(generation_id, None)

    async def close_all(self) -> None:
        errors: list[BaseException] = []
        for generation_id in list(self._catalogs):
            try:
                await self.close(generation_id)
            except RuntimeError as error:
                errors.append(error)
        if errors:
            raise RuntimeError(
                "MCP host shutdown failed: " + "; ".join(str(e) for e in errors)
            )

    def get(self, generation_id: str) -> PreparedMcpCatalog | None:
        return self._catalogs.get(generation_id)

    @staticmethod
    async def _disconnect_all(servers: Any) -> list[BaseException]:
        failures: list[BaseException] = []
        for server in servers:
            try:
                await server.client.disconnect()
            except BaseException as error:  # 清理阶段收集全部失败，不提前中断。
                failures.append(error)
        return failures

    @staticmethod
    def _validate_required_tools(
        servers: Mapping[str, PreparedMcpServer],
        required_tools: Mapping[str, Tuple[str, ...]],
    ) -> None:
        missing: list[str] = []
        for server_name, tool_names in required_tools.items():
            server = servers.get(server_name)
            if server is None:
                missing.append("server:%s" % server_name)
                continue
            available = set(server.remote_tool_names)
            missing.extend(
                "%s:%s" % (server_name, tool_name)
                for tool_name in tool_names
                if tool_name not in available
            )
        if missing:
            raise RuntimeError("required MCP tools missing: %s" % ", ".join(missing))
