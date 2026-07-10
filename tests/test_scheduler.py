"""Persistent delayed message scheduler tests."""

import asyncio
from datetime import datetime, timedelta
import json
import tempfile
import unittest
from pathlib import Path

from kirakira_agent.bus import MessageBus
from kirakira_agent.scheduler import SchedulerService
from kirakira_agent.tools.registry import ToolRegistry


class SchedulerTests(unittest.TestCase):
    def test_schedule_fires_to_bound_channel_and_persists_completion(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "schedules.json"
                bus = MessageBus()
                tools = ToolRegistry()
                tools.set_context(channel="telegram", chat_id="42")
                scheduler = SchedulerService(path, bus=bus, tools=tools)
                received = []

                async def collect(message):
                    received.append(message)
                    scheduler.stop()
                    bus.stop()

                bus.subscribe_outbound("telegram", collect)
                run_at = (datetime.now().astimezone() + timedelta(seconds=0.1)).isoformat()
                created = json.loads(
                    await scheduler.schedule("reminder", run_at=run_at)
                )
                tasks = [
                    asyncio.create_task(bus.dispatch_outbound()),
                    asyncio.create_task(scheduler.run()),
                ]
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

                self.assertEqual(received[0].content, "reminder")
                self.assertEqual(received[0].chat_id, "42")
                payload = json.loads(path.read_text(encoding="utf-8"))
                job = next(item for item in payload["jobs"] if item["id"] == created["id"])
                self.assertEqual(job["status"], "completed")

        asyncio.run(scenario())

    def test_cancelled_schedule_does_not_fire(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus()
                tools = ToolRegistry()
                tools.set_context(channel="web", chat_id="x")
                scheduler = SchedulerService(Path(tmp) / "schedules.json", bus=bus, tools=tools)
                created = json.loads(
                    await scheduler.schedule("later", delay_seconds=60)
                )

                result = scheduler.cancel_schedule(created["id"])

                self.assertIn("Cancelled", result)
                self.assertEqual(scheduler._due_jobs(), [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
