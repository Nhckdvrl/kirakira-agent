import asyncio
import pytest
from agent.composition import (
    ResourceScope,
    ServiceKey,
    ServiceRegistry,
    ServiceUnavailable,
)
from agent.plugins import PluginManager
from agent.tools.registry import ToolRegistry
from bus.event_bus import EventBus


@pytest.mark.asyncio
async def test_registry_borrow_is_bound_to_generation_and_contract():
    class Generation:
        def __init__(self):
            self.refs = 0

        def acquire(self):
            self.refs += 1

        def release(self):
            self.refs -= 1

    registry = ServiceRegistry()
    key = ServiceKey("demo.number.v1", int)
    old, new = Generation(), Generation()
    registry.publish("provider", {key: 1}, generation=old)
    async with registry.use(key) as value:
        registry.publish("provider", {key: 2}, generation=new)
        registry.withdraw("provider", generation=old)
        assert value == 1 and old.refs == 1
        async with registry.use(key) as current:
            assert current == 2
    assert old.refs == new.refs == 0
    with pytest.raises(TypeError):
        registry.publish("provider", {ServiceKey("demo.number.v1", str): "wrong ABI"})
    with pytest.raises(ValueError):
        registry.publish("rival", {key: 3})
    registry.withdraw("provider")
    with pytest.raises(ServiceUnavailable):
        async with registry.use(key):
            pass


@pytest.mark.asyncio
async def test_scope_cancellation_waits_for_task_finally_and_closes_all_resources():
    scope = ResourceScope()
    started = asyncio.Event()
    settle = asyncio.Event()
    events = []

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await settle.wait()
            events.append("task-finally")

    scope.spawn(work())
    await started.wait()
    scope.on_close(lambda: events.append("resource"))
    closing = asyncio.create_task(scope.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not events
    settle.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert events == ["task-finally", "resource"]
    await scope.close()


@pytest.mark.asyncio
async def test_real_provider_and_consumer_dependency_publication(tmp_path):
    plugins = tmp_path / "plugins"
    for name, code in {
        "z_provider": "from agent.plugins import Plugin, ServiceKey\nclass P(Plugin):\n name='z_provider'\n async def prepare(self): self.context.provide(ServiceKey('demo.number.v1', int), 41)\n",
        "a_consumer": "from agent.plugins import Plugin, ServiceKey\nclass C(Plugin):\n name='a_consumer'\n @classmethod\n def requires(cls): return (ServiceKey('demo.number.v1', int),)\n",
    }.items():
        root = plugins / name
        root.mkdir(parents=True)
        (root / "plugin.py").write_text(code)
    manager = PluginManager(
        [plugins],
        workspace=tmp_path,
        event_bus=EventBus(),
        tool_registry=ToolRegistry(),
        session_manager=None,
        memory=None,
    )
    await manager.load_all()
    published = await manager.publish_generations()
    assert len(published) == 2 and not manager.errors
    consumer = next(r.instance for r in manager.active if r.plugin_id == "a_consumer")
    async with consumer.context.use(ServiceKey("demo.number.v1", int)) as value:
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
        session_manager=None,
        memory=None,
    )
    await manager.load_all()
    assert await manager.publish_generations() == []
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
