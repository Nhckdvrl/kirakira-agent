"""Installed code, capabilities and effects use the production dependency graph."""

import asyncio

import pytest

from agent.composition import ServiceKey
from agent.plugins import PluginManager
from bus.event_bus import EventBus
from bus.events import InboundMessage
from core.schema import ModelResponse, ToolCall, ToolSpec
from plugins.tools.registry import ToolMenu, ToolRegistry
from tests.test_runtime import FakeModel, _tool_result_for, build_test_runtime


def write_plugin(base, name, text):
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.py").write_text(text)
    return directory


def manager_at(tmp_path):
    tools = ToolRegistry()
    return PluginManager(
        [tmp_path / "plugins"],
        workspace=tmp_path,
        event_bus=EventBus(),
        tool_registry=tools,
    ), tools


@pytest.mark.asyncio
async def test_native_module_apply_and_dependency_replacement_rebuild_actual_consumers(
    tmp_path,
):
    directory = tmp_path / "plugins"

    def provider(value):
        return f"""from agent.composition import ServiceKey
name = "provider"
api_version = 3
async def apply(ctx):
    await ctx.provide(ServiceKey("example.number.v1"), {value})
"""

    write_plugin(directory, "provider", provider(1))
    write_plugin(
        directory,
        "consumer",
        """from agent.composition import ServiceKey
name = "consumer"
inject = (ServiceKey("example.number.v1"),)
async def apply(ctx):
    value = ctx.require(inject[0])
    await ctx.provide(ServiceKey("example.derived.v1"), value + 10)
""",
    )
    write_plugin(
        directory,
        "peer",
        """from agent.composition import ServiceKey
name = "peer"
async def apply(ctx):
    await ctx.provide(ServiceKey("example.peer.v1"), object())
""",
    )
    manager, tools = manager_at(tmp_path)
    await manager.load_all()
    try:
        assert not manager.errors
        assert manager.services.resolve(ServiceKey("example.derived.v1")) == 11
        from agent.plugin_composition import CompositionError

        async with manager.services.use(ServiceKey("example.number.v1")):
            with pytest.raises(CompositionError, match="卸载"):
                await manager.root.remove("plugin:provider")
        old_consumer = manager.root.activation("plugin:consumer")
        old_peer = manager.root.activation("plugin:peer")
        peer_value = manager.services.resolve(ServiceKey("example.peer.v1"))
        write_plugin(directory, "provider", provider(2))
        await manager.reconcile_changed()
        assert manager.services.resolve(ServiceKey("example.derived.v1")) == 12
        assert old_consumer.state == "closed"
        assert manager.root.activation("plugin:consumer") is not old_consumer
        assert manager.root.activation("plugin:peer") is old_peer
        assert manager.services.resolve(ServiceKey("example.peer.v1")) is peer_value
    finally:
        await manager.terminate_all()
        await tools.shutdown()


@pytest.mark.asyncio
async def test_running_tools_hold_only_their_activation_and_stale_proposals_cannot_execute(
    tmp_path,
):
    source = tmp_path / "plugins"
    write_plugin(
        source,
        "reader",
        """from agent.plugins import Plugin
from core.schema import ToolSpec
class Reader(Plugin):
    name = "reader"
    def register_tools(self, tools):
        async def read(index):
            gate = self.context.event_bus.gate
            gate[0].append(index)
            if len(gate[0]) == 2: gate[1].set()
            await gate[2].wait()
            return "v1"
        tools.register(ToolSpec("reader", "read", {"type":"object"}), read, risk="read-only", parallel_safe=True)
""",
    )
    manager, tools = manager_at(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    manager.event_bus.gate = ([], entered, release)
    await manager.load_all()
    old = manager.root.activation("plugin:reader")
    menu = ToolMenu(tools)
    tasks = [
        asyncio.create_task(
            menu.execute_async(ToolCall(str(i), "reader", {"index": i}))
        )
        for i in range(2)
    ]
    changing = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert old.refs == 2
        path = source / "reader" / "plugin.py"
        path.write_text(path.read_text().replace('return "v1"', 'return "v2"'))
        changing = asyncio.create_task(manager.reconcile_changed())
        for _ in range(100):
            if old.state == "draining":
                break
            await asyncio.sleep(0.001)
        assert old.state == "draining" and not changing.done()
        stale = await menu.execute_async(ToolCall("late", "reader", {"index": 99}))
        assert stale.is_error and manager.event_bus.gate[0] == [0, 1]
        tools.register(ToolSpec("unrelated", "ok", {}), lambda: "available")
        assert (
            await tools.execute_async(ToolCall("peer", "unrelated", {}))
        ).content == "available"
        release.set()
        assert [r.content for r in await asyncio.gather(*tasks)] == ["v1", "v1"]
        await changing
        assert old.state == "closed" and old.refs == 0
        assert (
            await menu.execute_async(ToolCall("stale", "reader", {"index": 4}))
        ).is_error
        assert (
            await tools.execute_async(ToolCall("new", "reader", {"index": 5}))
        ).content == "v2"
    finally:
        release.set()
        await asyncio.gather(
            *tasks, *([changing] if changing else []), return_exceptions=True
        )
        await manager.terminate_all()
        await tools.shutdown()


@pytest.mark.asyncio
async def test_new_apply_failure_withdraws_only_affected_capabilities(tmp_path):
    directory = tmp_path / "plugins"
    write_plugin(
        directory,
        "value",
        """from agent.composition import ServiceKey
name="value"
async def apply(ctx): await ctx.provide(ServiceKey("example.value.v1"), 1)
""",
    )
    write_plugin(
        directory,
        "peer",
        """from agent.composition import ServiceKey
name="peer"
async def apply(ctx): await ctx.provide(ServiceKey("example.peer.v1"), object())
""",
    )
    manager, tools = manager_at(tmp_path)
    await manager.load_all()
    peer = manager.services.resolve(ServiceKey("example.peer.v1"))
    write_plugin(
        directory,
        "value",
        'name="value"\nasync def apply(ctx): raise RuntimeError("new activation failed")\n',
    )
    try:
        await manager.reconcile_changed()
        assert "new activation failed" in manager.errors["value"]
        assert not manager.services.contains(ServiceKey("example.value.v1"))
        assert manager.services.resolve(ServiceKey("example.peer.v1")) is peer
        assert manager.root.activation("plugin:value").state == "failed"
    finally:
        await manager.terminate_all()
        await tools.shutdown()


@pytest.mark.asyncio
async def test_conversation_gets_fresh_menu_after_local_tool_update(tmp_path):
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall("change", "change_catalog", {})]),
            ModelResponse(
                tool_calls=[
                    ToolCall("search", "tool_search", {"query": "select:new_read"})
                ]
            ),
            ModelResponse(tool_calls=[ToolCall("read", "new_read", {})]),
            ModelResponse(text="done"),
        ]
    )
    _, loop, sessions, memory = build_test_runtime(tmp_path, model)
    tools = loop.pipeline.tools
    tools.register(ToolSpec("old_read", "old", {}), lambda: "old", deferred=True)

    def change():
        tools.unregister("old_read")
        tools.register(ToolSpec("new_read", "new", {}), lambda: "new", deferred=True)
        return "updated"

    tools.register(ToolSpec("change_catalog", "change", {}), change)
    try:
        result = await loop.pipeline.run(
            InboundMessage("cli", "user", "menu", "read"),
            "cli:menu",
            dispatch_outbound=False,
        )
        assert result.content == "done"
        assert _tool_result_for(sessions, "cli:menu", "new_read") == "new"
        assert "old_read" not in {s.name for s in model.calls[-1]["tools"]}
    finally:
        await tools.shutdown()
        await memory.shutdown()
        sessions.close()


import pytest

from agent.composition import (
    ResourceScope,
)


@pytest.mark.asyncio
async def test_real_provider_and_consumer_dependency_publication(tmp_path):
    plugins = tmp_path / "plugins"
    for name, code in {
        "z_provider": "from agent.plugins import Plugin, ServiceKey\nclass P(Plugin):\n name='z_provider'\n async def prepare(self): self.context.provide(ServiceKey('demo.number.v1'), 41)\n",
        "a_consumer": "from agent.plugins import Plugin, ServiceKey\nclass C(Plugin):\n name='a_consumer'\n @classmethod\n def requires(cls): return (ServiceKey('demo.number.v1'),)\n",
    }.items():
        root = plugins / name
        root.mkdir(parents=True)
        (root / "plugin.py").write_text(code)
    manager = PluginManager(
        [plugins],
        workspace=tmp_path,
        event_bus=EventBus(),
        tool_registry=ToolRegistry(),
    )
    await manager.load_all()
    assert len(manager.active) == 2 and not manager.errors
    consumer = next(r.instance for r in manager.active if r.plugin_id == "a_consumer")
    async with consumer.context.use(ServiceKey("demo.number.v1")) as value:
        assert value == 41
    with pytest.raises(PermissionError):
        consumer.context.use(ServiceKey("demo.undeclared.v1"))
    await manager.terminate_all()
    assert manager.services.catalog() == ()


@pytest.mark.asyncio
async def test_missing_capability_withdraws_unpublished_tools_and_resources(tmp_path):
    root = tmp_path / "plugins" / "broken"
    root.mkdir(parents=True)
    (root / "plugin.py").write_text(
        "from agent.plugins import Plugin, ServiceKey\n"
        "from core.schema import ToolSpec\nclass Broken(Plugin):\n name='broken'\n"
        " @classmethod\n def requires(cls): return (ServiceKey('missing.service.v1'),)\n"
        " def register_tools(self, registry): registry.register(ToolSpec('bad_tool','bad',{}), lambda: 'bad')\n"
    )
    registry = ToolRegistry()
    manager = PluginManager(
        [root.parent],
        workspace=tmp_path,
        event_bus=EventBus(),
        tool_registry=registry,
    )
    await manager.load_all()
    assert not registry.has("bad_tool") and manager.active == []
    assert "missing" in manager.errors["broken"]
    await manager.terminate_all()


@pytest.mark.asyncio
async def test_cancelled_resource_closer_does_not_skip_remaining_resources():
    scope = ResourceScope()
    events = []
    scope.on_close(lambda: events.append("remaining resource"))

    async def cancelled_closer():
        raise asyncio.CancelledError

    scope.on_close(cancelled_closer)
    with pytest.raises(ExceptionGroup, match="resource cleanup"):
        await scope.close()
    assert events == ["remaining resource"]
