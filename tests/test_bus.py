"""Message bus ordering and cross-chat concurrency tests."""

import asyncio
import unittest

from bus.events import OutboundMessage
from bus.queue import MessageBus, OutboundDeliveryError


class BusTests(unittest.TestCase):
    def test_outbound_is_ordered_per_chat_and_concurrent_across_chats(self):
        async def scenario():
            bus = MessageBus()
            events = []

            async def send(message):
                events.append(("start", message.chat_id, message.content))
                if message.content == "first":
                    await asyncio.sleep(0.05)
                elif message.content == "other":
                    await asyncio.sleep(0.01)
                events.append(("end", message.chat_id, message.content))

            bus.subscribe_outbound("test", send)
            dispatcher = asyncio.create_task(bus.dispatch_outbound())
            await bus.publish_outbound(OutboundMessage("test", "same", "first"))
            await bus.publish_outbound(OutboundMessage("test", "same", "second"))
            await bus.publish_outbound(OutboundMessage("test", "different", "other"))
            self.assertTrue(await bus.drain(timeout=2))
            bus.stop()
            await dispatcher

            self.assertLess(
                events.index(("end", "different", "other")),
                events.index(("end", "same", "first")),
            )
            self.assertLess(
                events.index(("end", "same", "first")),
                events.index(("start", "same", "second")),
            )

        asyncio.run(scenario())

    def test_waitable_delivery_completes_after_channel_callback(self):
        async def scenario():
            bus = MessageBus()
            sent = []

            async def send(message):
                await asyncio.sleep(0.01)
                sent.append(message.content)

            bus.subscribe_outbound("test", send)
            dispatcher = asyncio.create_task(bus.dispatch_outbound())
            await bus.publish_outbound_and_wait(
                OutboundMessage("test", "chat", "confirmed")
            )
            bus.stop()
            await dispatcher
            self.assertEqual(sent, ["confirmed"])

        asyncio.run(scenario())

    def test_waitable_delivery_fails_without_channel_subscriber(self):
        async def scenario():
            bus = MessageBus()
            dispatcher = asyncio.create_task(bus.dispatch_outbound())
            with self.assertRaises(OutboundDeliveryError):
                await bus.publish_outbound_and_wait(
                    OutboundMessage("missing", "chat", "lost")
                )
            bus.stop()
            await dispatcher

        asyncio.run(scenario())


import unittest
from dataclasses import dataclass

from bus.event_bus import EventBus


@dataclass
class Event:
    value: int


class EventBusTests(unittest.TestCase):
    def test_emit_is_ordered_and_enqueue_is_awaitable_on_shutdown(self):
        async def scenario():
            bus = EventBus()
            observed = []

            def first(event):
                event.value += 1
                return event

            async def second(event):
                await asyncio.sleep(0.01)
                event.value *= 2
                observed.append(event.value)
                return event

            bus.on(Event, first)
            bus.on(Event, second)
            emitted = await bus.emit(Event(2))
            self.assertEqual(emitted.value, 6)

            bus.enqueue(Event(3))
            await bus.shutdown()
            self.assertIn(8, observed)

        asyncio.run(scenario())
