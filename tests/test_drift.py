"""Drift 链路测试：skill 发现、连续性状态、端到端一轮 run。"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kirakira_agent.bus import MessageBus
from kirakira_agent.drift.runner import DriftRunner
from kirakira_agent.drift.skills import discover_skills, ensure_example_skill
from kirakira_agent.drift.state import DriftStateStore
from kirakira_agent.proactive.config import DriftConfig
from kirakira_agent.schema import ModelResponse, ToolCall
from kirakira_agent.session import SessionManager

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


class SkillDiscoveryTests(unittest.TestCase):
    def test_ensure_and_discover_example(self):
        tmp = tempfile.TemporaryDirectory()
        workdir = Path(tmp.name)
        ensure_example_skill(workdir)
        skills = discover_skills(workdir)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "explore-curiosity")
        self.assertIn("finish_drift", skills[0].body)
        tmp.cleanup()


class DriftStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DriftStateStore(Path(self.tmp.name) / "drift.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_min_interval_gating(self):
        self.assertTrue(self.store.can_run(NOW, 3.0))
        self.store.record_run(
            skill="s", now=NOW, status="completed", briefing="b", message_result="silent"
        )
        self.assertFalse(self.store.can_run(NOW + timedelta(hours=1), 3.0))
        self.assertTrue(self.store.can_run(NOW + timedelta(hours=4), 3.0))

    def test_continuum_roundtrip(self):
        self.store.save_continuum(
            skill="s", now=NOW, scratchpad="从第3步继续", next_tendency="想问音乐"
        )
        got = self.store.get_continuum("s")
        self.assertEqual(got["scratchpad"], "从第3步继续")
        self.assertEqual(got["next_tendency"], "想问音乐")


class _ScriptedClient:
    """驱动 agent loop：先 message_push，再 finish_drift，最后收尾。"""

    def __init__(self):
        self._step = 0

    def complete(self, messages, tools, system, model, max_tokens):
        self._step += 1
        if self._step == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(id="t1", name="message_push", arguments={"message": "最近在听什么歌？"})
                ]
            )
        if self._step == 2:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="finish_drift",
                        arguments={"status": "completed", "briefing": "问了音乐话题"},
                    )
                ]
            )
        return ModelResponse(text="done", stop_reason="end_turn")


class DriftRunnerTests(unittest.TestCase):
    def test_end_to_end_run_pushes_and_records(self):
        async def scenario():
            tmp = tempfile.TemporaryDirectory()
            workdir = Path(tmp.name)
            sessions = SessionManager(workdir)
            bus = MessageBus()
            sent = []
            bus.subscribe_outbound("web", lambda m: sent.append(m) or asyncio.sleep(0))
            runner = DriftRunner(
                config=DriftConfig(enabled=True, min_interval_hours=0, max_steps=6),
                workspace=workdir,
                bus=bus,
                session_manager=sessions,
                model_client=_ScriptedClient(),
                model="fake",
                memory=None,
                target_channel="web",
                target_chat_id="u1",
            )
            dispatcher = asyncio.create_task(bus.dispatch_outbound())
            ran = await runner.maybe_run(NOW, "web:u1")
            await bus.drain(timeout=2)
            bus.stop()
            await dispatcher
            recent = runner._state.recent_runs()
            runner.close()
            sessions.close()
            tmp.cleanup()
            return ran, sent, recent

        ran, sent, recent = asyncio.run(scenario())
        self.assertTrue(ran)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].content, "最近在听什么歌？")
        self.assertTrue(sent[0].metadata.get("drift"))
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["status"], "completed")
        self.assertEqual(recent[0]["message_result"], "sent")

    def test_disabled_does_not_run(self):
        async def scenario():
            tmp = tempfile.TemporaryDirectory()
            workdir = Path(tmp.name)
            sessions = SessionManager(workdir)
            runner = DriftRunner(
                config=DriftConfig(enabled=False),
                workspace=workdir,
                bus=MessageBus(),
                session_manager=sessions,
                model_client=_ScriptedClient(),
                model="fake",
            )
            ran = await runner.maybe_run(NOW, "web:u1")
            runner.close()
            sessions.close()
            tmp.cleanup()
            return ran

        self.assertFalse(asyncio.run(scenario()))


if __name__ == "__main__":
    unittest.main()
