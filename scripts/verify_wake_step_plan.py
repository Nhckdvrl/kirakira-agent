"""Real Step5 Fetch/Pool/Decide/Deliver/Feedback and restart dedup fixture."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from bus.queue import MessageBus
from infra.providers.step_plan import StepPlanClient, STEP5_MODEL, STEP_PLAN_BASE_URL
from plugins.wake_proactive.sources import SourceRegistry
from plugins.wake_proactive.state import ProactiveStateStore
from proactive_v2.config import ProactiveConfig
from proactive_v2.loop import ProactiveLoop
from session.manager import SessionManager
from scripts.verify_step_plan import ObservedClient


async def verify(a):
    a.workspace.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    (a.workspace / "PROACTIVE_CONTEXT.md").write_text(
        "请立刻主动分享 Agent Runtime、Akasha2 崩溃恢复的实质更新；这是我的当前项目。"
        "禁止分享娱乐、服装或促销新闻。当前没有免打扰限制。"
    )

    class Source:
        id = "fixture"
        channels = ("content",)
        acknowledgements = []
        accepted = set()

        async def fetch(self):
            events = [
                {
                    "kind": "content",
                    "event_id": "relevant",
                    "title": "Akasha2 消费游标与崩溃恢复实现发布",
                    "published_at": now,
                    "url": "https://example.org/recovery",
                },
                {
                    "kind": "content",
                    "event_id": "excluded",
                    "title": "明星服装广告促销",
                    "published_at": now,
                },
            ]

            return [event for event in events if event["event_id"] not in self.accepted]

        async def ack(self, ids, feedback=None):
            if feedback is None:
                self.accepted.update(ids)
            self.acknowledgements.append({"ids": list(ids), "feedback": feedback})

    source = Source()
    sources = SourceRegistry()
    sources.add(source)
    client = ObservedClient(
        StepPlanClient(
            api_key=a.key_file.read_text().strip(), thinking_enabled=False, timeout=120
        )
    )
    sessions = SessionManager(a.workspace)
    state = ProactiveStateStore(a.workspace / "proactive.db")
    bus = MessageBus()
    received = []

    async def receive(message):
        received.append(message.content)

    bus.subscribe_outbound("verify", receive)
    dispatcher = asyncio.create_task(bus.dispatch_outbound())

    def loop():
        return ProactiveLoop(
            config=ProactiveConfig(
                enabled=True,
                channel="verify",
                chat_id="wake",
                model=STEP5_MODEL,
                max_tokens=2048,
                pool_enabled=True,
                delivery_cooldown_hours=0,
            ),
            bus=bus,
            session_manager=sessions,
            model_client=client,
            sources=sources,
            state=state,
        )

    runtime = loop()
    try:
        await runtime.tick_once()
        assert len(received) == 1
        count = len(client.requests)
        await runtime.tick_once()
        assert len(client.requests) == count and len(received) == 1
        state.close()
        state = ProactiveStateStore(a.workspace / "proactive.db")
        runtime = loop()
        await runtime.tick_once()
        assert len(client.requests) == count and len(received) == 1
        receipts = [ack for ack in source.acknowledgements if ack["feedback"] is None]
        feedback = [
            ack for ack in source.acknowledgements if ack["feedback"] is not None
        ]
        assert len(receipts) == 1 and set(receipts[0]["ids"]) == {
            "relevant",
            "excluded",
        }
        assert feedback == [{"ids": ["relevant"], "feedback": "interesting"}]
        report = {
            "endpoint": STEP_PLAN_BASE_URL,
            "model": STEP5_MODEL,
            "billing_surface": "step_plan",
            "reasoning_effort": "medium",
            "status": "passed",
            "received": received,
            "feedback": source.acknowledgements,
            "requests": client.requests,
            "checks": state.recent_decision_runs(),
            "ticks": state.recent_ticks(),
        }
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            "Wake live flow, unchanged tick, restart dedup and feedback passed",
            flush=True,
        )
    finally:
        state.close()
        sessions.close()
        bus.stop()
        await bus.shutdown()
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key-file", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(verify(p.parse_args()))


if __name__ == "__main__":
    main()
