"""Behavioral gates for proactive policy, durable admission and recovery."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.schema import ModelResponse, ToolCall, ToolSpec
from plugins.proactive_flow.judge import ProactiveJudge
from plugins.proactive_flow.modules import ContentModule
from plugins.react.policy import ReasoningPolicy
from plugins.reply_program.scoped import run_scoped
from plugins.tools.registry import ToolRegistry
from plugins.wake_proactive.state import ProactiveStateStore
from proactive_v2.config import ProactiveConfig
from proactive_v2.contracts import normalize_content
from proactive_v2.frame import new_proactive_frame

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def acomplete(self, messages, tools, system, model, max_tokens, **kwargs):
        self.calls.append([spec.name for spec in tools])
        return next(self.responses, ModelResponse(text="done"))


def decision(response):
    async def scenario():
        client = Script([response])
        judge = ProactiveJudge(client, model="fake", max_tokens=100)
        result = await judge.decide_content(
            [normalize_content({"item_id": "source:1", "title": "music"})],
            memory_text="likes music",
            recent_conversation="",
            proactive_context="",
            current_context="",
        )
        assert all(
            set(names) == {"share_content", "skip_content"} for names in client.calls
        )
        return result

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(
            text='{"decision":"send","message":"hello","cited_ids":["source:1"]}'
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "a", "share_content", {"message": "hello", "items": ["foreign:1"]}
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "a",
                    "share_content",
                    {"message": "hello", "items": ["source:1", "source:1"]},
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "a", "share_content", {"message": "hello", "items": ["source:1"]}
                ),
                ToolCall("b", "skip_content", {"reason": "conflict"}),
            ]
        ),
        ModelResponse(
            tool_calls=[ToolCall("a", "message_push", {"message": "bypass"})]
        ),
    ],
)
def test_invalid_or_conflicting_decisions_never_authorize_delivery(response):
    result = decision(response)
    assert not result.send
    assert result.status == "invalid"


def test_exact_frozen_reference_authorizes_one_message():
    result = decision(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "a",
                    "share_content",
                    {
                        "message": "hello",
                        "items": ["source:1"],
                    },
                )
            ]
        )
    )
    assert result.send and result.cited_ids == ["source:1"]
    assert result.trace["tool_chain"][0]["calls"][0]["status"] == "success"


def test_unadvertised_tool_cannot_bypass_grant_even_if_registered():
    async def scenario():
        registry = ToolRegistry()
        executed = []
        registry.register(ToolSpec("secret", "", {}), lambda: executed.append(True))
        client = Script([ModelResponse(tool_calls=[ToolCall("x", "secret", {})])])
        result = await run_scoped(
            model_client=client,
            registry=registry,
            model="fake",
            system="",
            prompt="",
            session_key="test",
            max_steps=1,
            max_tokens=100,
            policy=ReasoningPolicy(
                allowed_tools=frozenset(), summarize_on_exhaustion=False
            ),
        )
        assert not executed
        assert client.calls == [[]]
        assert result.tool_chain[0]["calls"][0]["status"] != "success"

    asyncio.run(scenario())


def test_narrowed_grant_applies_to_later_calls_in_the_same_batch():
    async def scenario():
        registry = ToolRegistry()
        executed = []
        grant = {"stage", "danger", "finish"}

        def stage():
            executed.append("stage")
            grant.intersection_update({"finish"})
            return "staged"

        registry.register(ToolSpec("stage", "", {}), stage)
        registry.register(ToolSpec("danger", "", {}), lambda: executed.append("danger"))
        client = Script(
            [
                ModelResponse(
                    tool_calls=[ToolCall("1", "stage", {}), ToolCall("2", "danger", {})]
                )
            ]
        )
        result = await run_scoped(
            model_client=client,
            registry=registry,
            model="fake",
            system="",
            prompt="",
            session_key="test",
            max_steps=1,
            max_tokens=100,
            policy=ReasoningPolicy(
                current_tools=lambda: frozenset(grant), summarize_on_exhaustion=False
            ),
        )
        assert executed == ["stage"]
        assert result.tool_chain[0]["calls"][1]["status"] != "success"

    asyncio.run(scenario())


def test_cooldown_arrival_survives_restart_without_another_new_event(tmp_path):
    async def scenario():
        path = tmp_path / "proactive.db"
        store = ProactiveStateStore(path)
        event = {
            "item_id": "source:1",
            "_source": "source",
            "event_id": "1",
            "title": "music",
        }
        store.ingest("content", [event], NOW)
        store.mark_push("web:one", NOW)
        called = []

        async def push(contents, *_args, **_kwargs):
            called.extend(contents)
            return True

        async def interest(*_args):
            return ""

        loop = SimpleNamespace(
            _cfg=ProactiveConfig(channel="web", chat_id="one"),
            _state=store,
            _push_content=push,
            _interest_hits=interest,
        )
        await ContentModule(loop).run(new_proactive_frame("web:one", now=NOW))
        assert not called
        store.close()
        loop._state = ProactiveStateStore(path)
        # No ingest:new_content slot. The durable pending fact is sufficient.
        await ContentModule(loop).run(
            new_proactive_frame("web:one", now=NOW + timedelta(hours=2))
        )
        assert [item["item_id"] for item in called] == ["source:1"]
        loop._state.close()

    asyncio.run(scenario())


def test_skip_is_checked_once_failure_retries_after_durable_delay(tmp_path):
    path = tmp_path / "proactive.db"
    store = ProactiveStateStore(path)
    event = {"item_id": "source:1", "title": "music"}
    store.ingest("content", [event], NOW)
    store.record_content_check(
        "web:one", [event], {"status": "error"}, NOW, retry_seconds=300
    )
    store.close()
    store = ProactiveStateStore(path)
    assert not store.pending_content(NOW + timedelta(seconds=299))
    assert store.pending_content(NOW + timedelta(seconds=300))
    store.record_content_check(
        "web:one", [event], {"status": "valid", "send": False}, NOW
    )
    assert not store.pending_content(NOW + timedelta(days=1))
    assert store.unread_count("content") == 1
    assert len(store.recent_decision_runs()) == 2
    store.close()
