from agent.composition.lifecycle import on_close, on_start, on_stopping

"""Application composition root. Channel and proactive wiring have independent owners."""


from bus.contract import BUS, EVENTS
from bus.event_bus import EventBus
from bus.queue import MessageBus

inject = ()
provides = (BUS, EVENTS)


async def apply(ctx):
    ctx.config["workspace"]
    ctx.config["app_config"]
    bus = MessageBus()
    event_bus = EventBus()
    await on_close(ctx, lambda: event_bus.shutdown())
    await on_close(ctx, lambda: bus.shutdown())

    await ctx.provide(BUS, bus)
    await ctx.provide(EVENTS, event_bus)

    await on_start(ctx, lambda: ctx.spawn(bus.dispatch_outbound(), name="bus_dispatch"))
    await on_stopping(ctx, lambda: bus.drain(timeout=10.0))
