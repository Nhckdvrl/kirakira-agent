"""Persistent MCP server lifecycle and ToolRegistry integration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from kirakira_agent.mcp.client import McpClient
from kirakira_agent.schema import ToolSpec
from kirakira_agent.tools.registry import ToolRegistry, object_schema

logger = logging.getLogger(__name__)


class McpServerRegistry:
    def __init__(self, config_path: Path, tools: ToolRegistry) -> None:
        self.config_path = config_path
        self.tools = tools
        self._clients: Dict[str, McpClient] = {}
        self._server_tools: Dict[str, List[str]] = {}
        self._plugin_servers: set[str] = set()
        self._lock = asyncio.Lock()
        self._register_management_tools()

    async def load_and_connect_all(self) -> None:
        configs = self._load_configs()

        async def connect(name: str, config: Dict[str, Any]) -> None:
            try:
                await self._connect(
                    name,
                    list(config.get("command") or []),
                    dict(config.get("env") or {}),
                    str(config.get("cwd") or "") or None,
                )
            except Exception:
                logger.exception("failed to connect MCP server %s", name)

        await asyncio.gather(*(connect(name, cfg) for name, cfg in configs.items()))

    async def add(
        self,
        name: str,
        command: List[str],
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> str:
        name = self._validate_name(name)
        async with self._lock:
            if name in self._clients:
                return "Error: MCP server %r already exists" % name
            names = await self._connect(name, command, env or {}, cwd)
            self._save()
        return "Connected MCP server %r with tools: %s" % (name, ", ".join(names))

    async def remove(self, name: str) -> str:
        async with self._lock:
            if name not in self._clients:
                return "Error: MCP server %r does not exist" % name
            await self._disconnect(name)
            self._plugin_servers.discard(name)
            self._save()
        return "Removed MCP server %r" % name

    def list_servers(self) -> str:
        payload = [
            {"name": name, "tools": list(self._server_tools.get(name, []))}
            for name in sorted(self._clients)
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def sync_plugin_servers(self, configs: Dict[str, Dict[str, Any]]) -> None:
        async with self._lock:
            desired = set(configs)
            for name in sorted(self._plugin_servers - desired):
                await self._disconnect(name)
                self._plugin_servers.discard(name)
            for name in sorted(desired - self._plugin_servers):
                if name in self._clients:
                    logger.warning("plugin MCP server name already exists: %s", name)
                    continue
                config = configs[name]
                try:
                    await self._connect(
                        self._validate_name(name),
                        list(config.get("command") or []),
                        dict(config.get("env") or {}),
                        str(config.get("cwd") or "") or None,
                    )
                except Exception:
                    logger.exception("failed to connect plugin MCP server %s", name)
                    continue
                self._plugin_servers.add(name)

    async def shutdown(self) -> None:
        async with self._lock:
            names = list(self._clients)
            await asyncio.gather(
                *(self._disconnect(name) for name in names), return_exceptions=True
            )

    async def _connect(
        self,
        name: str,
        command: List[str],
        env: Dict[str, str],
        cwd: Optional[str],
    ) -> List[str]:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("MCP command must be a non-empty string list")
        client = McpClient(name, command, env=env, cwd=cwd)
        infos = await client.connect()
        registered: List[str] = []
        try:
            for info in infos:
                tool_name = "mcp_%s__%s" % (
                    re.sub(r"[^a-zA-Z0-9_-]", "_", name),
                    re.sub(r"[^a-zA-Z0-9_-]", "_", info.name),
                )

                self.tools.register(
                    ToolSpec(
                        tool_name,
                        "[MCP:%s] %s" % (name, info.description),
                        info.input_schema,
                    ),
                    self._tool_handler(client, info.name),
                    deferred=True,
                )
                registered.append(tool_name)
        except Exception:
            for tool_name in registered:
                self.tools.unregister(tool_name)
            await client.disconnect()
            raise
        self._clients[name] = client
        self._server_tools[name] = registered
        return registered

    async def _disconnect(self, name: str) -> None:
        for tool_name in self._server_tools.pop(name, []):
            self.tools.unregister(tool_name)
        client = self._clients.pop(name, None)
        if client is not None:
            await client.disconnect()

    def _load_configs(self) -> Dict[str, Dict[str, Any]]:
        if not self.config_path.exists():
            return {}
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("failed to read MCP config %s", self.config_path)
            return {}
        servers = payload.get("servers", {}) if isinstance(payload, dict) else {}
        return servers if isinstance(servers, dict) else {}

    def _save(self) -> None:
        payload = {
            "servers": {
                name: {
                    "command": client.command,
                    "env": client.env,
                    "cwd": client.cwd,
                }
                for name, client in self._clients.items()
                if name not in self._plugin_servers
            }
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.config_path.with_name(
            ".%s.%d.%s.tmp" % (self.config_path.name, os.getpid(), uuid4().hex)
        )
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, self.config_path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _register_management_tools(self) -> None:
        self.tools.register(
            ToolSpec(
                "mcp_add",
                "Connect a local stdio MCP server and register its tools.",
                object_schema(
                    {
                        "name": {"type": "string"},
                        "command": {"type": "array", "items": {"type": "string"}},
                        "env": {"type": "object"},
                        "cwd": {"type": "string"},
                    },
                    ["name", "command"],
                ),
            ),
            self.add,
        )
        self.tools.register(
            ToolSpec(
                "mcp_remove",
                "Disconnect an MCP server and unregister its tools.",
                object_schema({"name": {"type": "string"}}, ["name"]),
            ),
            self.remove,
        )
        self.tools.register(
            ToolSpec(
                "mcp_list",
                "List connected MCP servers and tools.",
                object_schema({}, []),
            ),
            self.list_servers,
        )

    @staticmethod
    def _tool_handler(client: McpClient, tool_name: str):
        async def invoke(**kwargs: Any) -> str:
            return await client.call(tool_name, kwargs)

        return invoke

    @staticmethod
    def _validate_name(name: str) -> str:
        value = name.strip()
        if not value or not re.fullmatch(r"[a-zA-Z0-9_.-]+", value):
            raise ValueError("Invalid MCP server name: %r" % name)
        return value
