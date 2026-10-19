"""MCP connections own their calls and drain independently of other servers."""

import asyncio
import hashlib
import json
import logging
import re

from agent.composition import CompositionRoot, Module
from core.schema import ToolSpec
from plugins.mcp.host import McpGenerationHost
from plugins.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _sanitize(value):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


class McpCatalogPublisher:
    def __init__(
        self, tools: ToolRegistry, *, host=None, composition=None, parent=None
    ):
        self.tools = tools
        self._root = composition or CompositionRoot()
        self._parent = parent
        self._host = host or McpGenerationHost()
        self._sources = {}
        self._bindings = {}
        self._errors = {}
        self._lock = asyncio.Lock()

    @property
    def host(self):
        return self._host

    async def publish(self, server_specs, *, source="workspace"):
        async def publish():
            async with self._lock:
                proposed = {**self._sources, source: dict(server_specs)}
                merged = {}
                for specs in proposed.values():
                    for name, spec in specs.items():
                        if name in merged:
                            raise ValueError(
                                "MCP server name claimed by two sources: " + name
                            )
                        merged[name] = spec
                # Source selection precedes application. Connection failure does
                # not silently keep the previous implementation available.
                self._sources = proposed
                errors = []
                for name in sorted(set(self._bindings) | set(merged)):
                    old = self._bindings.get(name)
                    desired = merged.get(name)
                    signature = (
                        json.dumps(desired, sort_keys=True)
                        if desired is not None
                        else None
                    )
                    if (
                        old is not None
                        and old[0].state == "active"
                        and old[1] == signature
                    ):
                        continue
                    try:
                        if old is not None:
                            await self._close(name)
                        if desired is not None:
                            await self._open(name, desired, signature)
                        self._errors.pop(name, None)
                    except Exception as error:
                        self._errors[name] = str(error)
                        errors.append(error)
                if errors:
                    raise ExceptionGroup("MCP connection update failed", errors)
                return self.status()["generationId"]

        return await CompositionRoot._physical(publish())

    async def _open(self, name, spec, signature):
        async def apply(ctx):
            owner = ctx.owner
            self._bindings[name] = (owner, signature)
            # Register disposal before connecting; a partially acquired server
            # must stay owned if its physical cleanup fails.
            ctx.on_close(lambda: self._host.close(owner.generation_id))
            catalog = await self._host.prepare(
                owner.generation_id, server_specs={name: spec}, required_tools={}
            )
            staging = ToolRegistry()
            for server in catalog.servers.values():
                for info in server.tools:

                    async def call(_client=server.client, _tool=info.name, **kwargs):
                        return await _client.call(_tool, kwargs)

                    staging.register(
                        ToolSpec(
                            f"mcp_{_sanitize(server.name)}__{_sanitize(info.name)}",
                            f"[MCP:{server.name}] {info.description}",
                            info.input_schema,
                        ),
                        call,
                        deferred=True,
                        risk="external-side-effect",
                        source_type="mcp",
                        source_name=server.name,
                    )
            self.tools.install_owner(owner, staging)
            ctx.on_close(lambda: self.tools.remove_owner(owner))

        module = Module("mcp:" + name, apply)
        await self._root._mount(module, parent=self._parent)

    async def _close(self, name):
        await self._root.remove("mcp:" + name)
        self._bindings.pop(name)

    async def shutdown(self):
        async def close():
            async with self._lock:
                errors = []
                for name in tuple(self._bindings):
                    try:
                        await self._close(name)
                    except BaseException as error:
                        errors.append(error)
                if errors:
                    raise BaseExceptionGroup("MCP cleanup failed", errors)

        await CompositionRoot._physical(close())

    def status(self):
        active = {n: o for n, (o, _) in self._bindings.items() if o.state == "active"}
        revision = hashlib.sha256(
            "|".join(o.generation_id for o in active.values()).encode()
        ).hexdigest()[:16]
        return {
            "generationId": revision,
            "servers": sorted(active),
            "tools": sorted(
                n
                for n in self.tools.names()
                if self.tools.get_tool(n).meta.source_type == "mcp"
            ),
            "connections": {
                n: {"generationId": o.generation_id, "state": o.state, "calls": o.refs}
                for n, (o, _) in self._bindings.items()
            },
            "errors": dict(self._errors),
        }
