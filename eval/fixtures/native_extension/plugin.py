"""Evaluation-only real process, MCP and native tool contribution."""
import asyncio
from contextlib import asynccontextmanager
import json

from agent.plugin_composition import ServiceKey
from plugins.managed_processes.contract import MANAGED_PROCESSES, ManagedProcessDefinition
from plugins.mcp.contract import MCP_SERVERS, McpServerDefinition, EndpointEnv
from plugins.tools.contract import TOOLS, Result
from plugins.ledger.contract import ContentPart

api_version = 3
name = "native-eval"
version = "1.0.0"
inject = (MANAGED_PROCESSES, MCP_SERVERS, TOOLS)


async def apply(ctx):
    process = await ctx.require(MANAGED_PROCESSES).register(ctx, ManagedProcessDefinition(
        "health", ("./health.py",)))
    await ctx.require(MCP_SERVERS).register(ctx, McpServerDefinition(
        "native", ("./mcp_server.py",), required_tools=("echo",),
        endpoint_env=(EndpointEnv("NATIVE_PORT", process),),
    ))

    class Target:
        idempotent = False

        async def prepare(self, arguments, source=None):
            return dict(arguments)

        async def invoke(self, key, arguments):
            await asyncio.sleep(min(float(arguments.get("delay", 0)), 5))
            async with ctx.require(MCP_SERVERS).open(ctx, "native") as server:
                async with server.route() as route:
                    value = await route.call("echo", {"value": arguments["value"]})
            return Result("success" if value.success else "error", (ContentPart("text", value.output),))

        async def query(self, key):
            return None

    @asynccontextmanager
    async def open_tool(state):
        yield Target()

    catalog = ctx.require(TOOLS)
    await catalog.declare_group(ctx, always_on=True)
    await catalog.register(ctx, name="native_probe", description="Call a real MCP server backed by a managed HTTP process. Return its exact value.",
        parameters={"type": "object", "properties": {"value": {"type": "string"}, "delay": {"type": "number"}}, "required": ["value"]},
        open=open_tool)
    await ctx.provide(ServiceKey("eval.native.port"), process.port(ctx))
