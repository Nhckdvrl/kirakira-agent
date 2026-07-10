"""Tests for the Akashic-style passive runtime."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from kirakira_agent.bus import MessageBus
from kirakira_agent.context_builder import ContextBuilder
from kirakira_agent.event_bus import EventBus
from kirakira_agent.events import InboundMessage
from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.runtime import AgentLoop, DefaultReasoner, PassiveTurnPipeline, RuntimeConfig
from kirakira_agent.schema import ModelResponse, ToolCall
from kirakira_agent.session import SessionManager
from kirakira_agent.tool_hooks import HookOutcome
from kirakira_agent.tools import build_default_registry


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools, system, model, max_tokens):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        return self.responses.pop(0)


def build_test_runtime(workdir, model):
    bus = MessageBus()
    event_bus = EventBus()
    session_manager = SessionManager(workdir)
    memory = MemoryRuntime(workdir, session_manager=session_manager)
    tools = build_default_registry(workdir, memory=memory, session_manager=session_manager)
    context = ContextBuilder(workdir, memory)
    config = RuntimeConfig(model="fake", max_iterations=5, max_tokens=1000, history_window=20)
    reasoner = DefaultReasoner(
        model_client=model,
        tools=tools,
        config=config,
        context=context,
        event_bus=event_bus,
    )
    pipeline = PassiveTurnPipeline(
        bus=bus,
        event_bus=event_bus,
        session_manager=session_manager,
        memory=memory,
        tools=tools,
        reasoner=reasoner,
        config=config,
    )
    return bus, AgentLoop(bus=bus, pipeline=pipeline), session_manager, memory


class RuntimeTests(unittest.TestCase):
    def test_bus_to_loop_to_outbound_and_session_tool_chain(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                (workdir / "README.md").write_text("hello")
                model = FakeModel(
                    [
                        ModelResponse(tool_calls=[ToolCall("call_1", "read_file", {"path": "README.md"})]),
                        ModelResponse(text="读到了 hello。"),
                    ]
                )
                bus, loop, sessions, _memory = build_test_runtime(workdir, model)
                got = []

                async def collect(msg):
                    got.append(msg)
                    loop.stop()
                    bus.stop()

                bus.subscribe_outbound("cli", collect)
                tasks = [
                    asyncio.create_task(loop.run()),
                    asyncio.create_task(bus.dispatch_outbound()),
                ]
                await bus.publish_inbound(InboundMessage("cli", "tester", "chat", "read README"))
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

                self.assertEqual(got[0].content, "读到了 hello。")
                session = sessions.get_or_create("cli:chat")
                self.assertEqual(session.messages[-1]["tools_used"], ["read_file"])
                self.assertEqual(session.messages[-1]["tool_chain"][0]["calls"][0]["result"], "hello")

        asyncio.run(scenario())

    def test_explicit_memory_consolidates_after_turn(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                model = FakeModel([ModelResponse(text="记住了。")])
                bus, loop, _sessions, memory = build_test_runtime(workdir, model)

                async def collect(_msg):
                    loop.stop()
                    bus.stop()

                bus.subscribe_outbound("cli", collect)
                tasks = [
                    asyncio.create_task(loop.run()),
                    asyncio.create_task(bus.dispatch_outbound()),
                ]
                await bus.publish_inbound(InboundMessage("cli", "tester", "chat", "请记住：我喜欢蓝色"))
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

                recalled = memory.recall("蓝色")
                self.assertTrue(recalled)
                self.assertIn("蓝色", recalled[0].content)

        asyncio.run(scenario())

    def test_before_turn_module_can_abort(self):
        class AbortModule:
            async def run(self, ctx):
                ctx.abort = True
                ctx.abort_reply = "blocked by plugin"
                return ctx

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                model = FakeModel([ModelResponse(text="should not be used")])
                bus, loop, _sessions, _memory = build_test_runtime(workdir, model)
                loop.pipeline.add_before_turn_plugin_modules([AbortModule()])
                got = []

                async def collect(msg):
                    got.append(msg.content)
                    loop.stop()
                    bus.stop()

                bus.subscribe_outbound("cli", collect)
                tasks = [
                    asyncio.create_task(loop.run()),
                    asyncio.create_task(bus.dispatch_outbound()),
                ]
                await bus.publish_inbound(InboundMessage("cli", "tester", "chat", "hello"))
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

                self.assertEqual(got, ["blocked by plugin"])
                self.assertEqual(model.calls, [])

        asyncio.run(scenario())

    def test_tool_hook_can_deny_tool(self):
        class DenyReadFile:
            name = "deny_read_file"
            event = "pre_tool_use"

            def matches(self, ctx):
                return ctx.request.tool_name == "read_file"

            async def run(self, ctx):
                return HookOutcome(decision="deny", reason="read_file denied")

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                (workdir / "README.md").write_text("hello")
                model = FakeModel(
                    [
                        ModelResponse(tool_calls=[ToolCall("call_1", "read_file", {"path": "README.md"})]),
                        ModelResponse(text="工具被拦截了。"),
                    ]
                )
                bus, loop, sessions, _memory = build_test_runtime(workdir, model)
                loop.pipeline.reasoner.add_tool_hooks([DenyReadFile()])

                async def collect(_msg):
                    loop.stop()
                    bus.stop()

                bus.subscribe_outbound("cli", collect)
                tasks = [
                    asyncio.create_task(loop.run()),
                    asyncio.create_task(bus.dispatch_outbound()),
                ]
                await bus.publish_inbound(InboundMessage("cli", "tester", "chat", "read README"))
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

                session = sessions.get_or_create("cli:chat")
                call = session.messages[-1]["tool_chain"][0]["calls"][0]
                self.assertEqual(call["status"], "denied")
                self.assertEqual(call["result"], "read_file denied")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
