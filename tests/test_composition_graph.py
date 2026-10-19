from __future__ import annotations

import pytest

from agent.plugin_composition import CompositionRoot, EmitEventKey


@pytest.mark.asyncio
async def test_failed_consumer_cleanup_keeps_provider_until_explicit_retry():
    """消费者关闭失败后，原资源及依赖仍由同一 Root 持有。"""
    from agent.plugin_composition import ServiceKey

    root = CompositionRoot("cleanup-retry")
    service = ServiceKey[list[str]]("test.cleanup-resource")
    events: list[str] = []
    attempts = 0

    async def provider(ctx):
        await ctx.effect(lambda: lambda: events.append("provider-close"))
        await ctx.provide(service, events)

    async def consumer(ctx):
        resource = ctx.require(service)

        def close():
            nonlocal attempts
            attempts += 1
            resource.append("consumer-close")
            if attempts == 1:
                raise OSError("resource still open")

        await ctx.effect(lambda: close)

    await root.mount(provider, name="provider")
    await root.mount(consumer, name="consumer", inject=(service,))
    with pytest.raises(BaseExceptionGroup):
        await root.dispose()
    assert events == ["consumer-close"]

    await root.dispose()
    assert events == ["consumer-close", "consumer-close", "provider-close"]
    await root.dispose()
    assert events == ["consumer-close", "consumer-close", "provider-close"]


@pytest.mark.asyncio
async def test_emit_event_listener_failure_is_fail_loud() -> None:
    root = CompositionRoot("emit-event-failure")

    def fail(_: object) -> None:
        raise RuntimeError("observe failed")

    async def plugin(ctx) -> None:
        await ctx.on(EmitEventKey[object]("test.emit.failure"), fail)

    await root.mount(plugin, name="failing-emit-plugin")
    try:
        with pytest.raises(RuntimeError, match="observe failed"):
            root.context.emit(EmitEventKey[object]("test.emit.failure"), object())
    finally:
        await root.dispose()


@pytest.mark.asyncio
async def test_pending_initial_dependency_resolves_then_frozen_teardown_closes_consumers_first():
    """初始待定依赖可激活；退出顺序按实际依赖而非挂载顺序。"""
    from agent.plugin_composition import ServiceKey

    root = CompositionRoot("initial-resolution")
    service = ServiceKey[list[str]]("test.initial.service")
    events = []

    async def consumer(ctx):
        shared = ctx.require(service)
        events.append("consumer-start")
        await ctx.effect(lambda: lambda: shared.append("consumer-close"))

    async def provider(ctx):
        await ctx.effect(lambda: lambda: events.append("provider-close"))
        await ctx.provide(service, events)

    await root.mount(consumer, name="consumer", inject=(service,))
    assert events == []
    await root.mount(provider, name="provider")
    assert events == ["consumer-start"]
    await root.dispose()
    assert events == ["consumer-start", "consumer-close", "provider-close"]


@pytest.mark.asyncio
async def test_native_owner_call_blocks_only_its_actual_provider_and_rejects_stale_entrypoint():
    import asyncio

    from agent.plugin_composition import CompositionError, ServiceKey

    root = CompositionRoot("owner-call")
    service = ServiceKey("example.call")
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def provider(ctx):
        async def call():
            entered.set()
            await release.wait()
            return "done"

        await ctx.effect(lambda: lambda: closed.append(True))
        await ctx.provide(service, ctx.entrypoint(call))

    owner = await root.mount(provider, name="provider")
    old_call = root.service_value(service)
    calling = asyncio.create_task(old_call())
    await entered.wait()
    disposing = asyncio.create_task(owner.dispose())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not disposing.done() and not closed
    with pytest.raises(CompositionError):
        await old_call()
    release.set()
    assert await calling == "done"
    await disposing
    assert closed == [True]
    await root.dispose()


@pytest.mark.asyncio
async def test_native_task_cancellation_waits_for_physical_finally_before_resource_release():
    import asyncio

    root = CompositionRoot("physical-task")
    entered, cleanup, release = (asyncio.Event() for _ in range(3))
    events = []

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
            events.append("task-finally")

    async def provider(ctx):
        await ctx.effect(lambda: lambda: events.append("resource-close"))
        await ctx.spawn(work(), name="physical-work")

    await root.mount(provider, name="provider")
    await entered.wait()
    closing = asyncio.create_task(root.dispose())
    await cleanup.wait()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and events == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert events == ["task-finally", "resource-close"]
    await root.dispose()
