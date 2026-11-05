"""目标插件与默认回复链路的端到端组合测试。"""

from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from dataclasses import replace
from datetime import datetime, timezone

from agent.plugin_composition.config_input import save_config
from agent.plugins.manager import PluginManager
from plugins.channels.contract import CHANNEL_INPUT_V2 as CHANNEL_INPUT, ChannelInboundMessage
from plugins.goal.store import decode_goal, goal_key
from plugins.ledger.contract import MESSAGE_CATALOG, ContentPart, Input, Output
from tests.fixtures.plugin_workspace import initialize_plugin_workspace


_PROVIDER = r'''
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from agent.plugin_composition import ServiceKey
from plugins.models.contract import CHAT_MODELS, MODEL_CALL_STATS, MODEL_CATALOG
from plugins.models.contract import LLMResponse, ToolCall
from plugins.models.contract import (
    BoundModelDescriptor,
    CapabilitySources,
    ModelCapabilities,
)
from plugins.models.projection import (
    MODEL_CALLS, MODEL_PROJECTION, ProjectionOwner, MODEL_MESSAGE_CHECKS, MessageChecksOwner,
)
from plugins.models.content import MODEL_CONTENT, ContentOwner
from plugins.models.selection import MODEL_SELECTION, SelectionOwner
from plugins.models.state import _BoundChat, ModelsState
from plugins.models.settings import MODEL_SETTINGS
from plugins.models.store import ModelsStore
from plugins.tools.api import Result
from agent.plugin_composition.tasks import TASKS
from plugins.ledger.contract import BINDINGS
from plugins.tools.plugin import TOOLS
from plugins.ledger.contract import ContentPart

api_version = 3
name = "test_provider"
version = "1.0.0"
inject = (TOOLS, TASKS, BINDINGS)

async def apply(ctx):
    calls = []
    store = ModelsStore(ctx.data_root / "models.db", ctx.data_root / "backups")
    store.initialize()
    await ctx.effect(lambda: store.close, label="model-store")
    settings = ModelsState(store, context=ctx)
    await ctx.provide(MODEL_SETTINGS, settings.settings)
    await ctx.provide(MODEL_CATALOG, settings.catalog)

    class Driver:
        max_tool_schemas = None
        def estimate_context_tokens(self, messages, tools):
            return 10
        async def complete(self, request):
            calls.append(request)
            if len(calls) == 1:
                return LLMResponse(None, [ToolCall("call-1", "update_goal", {"status": "complete"})])
            return LLMResponse("finished")

    descriptor = BoundModelDescriptor(
        binding_id="fixture-model", plugin_snapshot_id="fixture", model_revision=0,
        model_id="fixture", connection_id="fixture", driver_id="fixture",
        driver_contract_version="1", auth_identity="fixture", model="fixture", role="agent",
        reasoning_effort=None, capabilities=ModelCapabilities(context_window=10000),
        capability_sources=CapabilitySources(), capability_digest="fixture",
    )
    model = _BoundChat(descriptor, Driver(), store)

    async def use(use, choice):
        return model

    class Models:
        @asynccontextmanager
        async def execution(self, *, model_id=None, reasoning_effort=None):
            async with ctx.runtime_scope():
                # This distribution's reply program binds a chat model by role,
                # so the fake accepts the role the caller actually passes.
                yield SimpleNamespace(chat=lambda _role=None: model, use=use)

    class Target:
        idempotent = False
        async def prepare(self, args, source=None):
            return args
        async def invoke(self, key, args):
            return Result("success", (ContentPart("text", "ok"),))
        async def query(self, key):
            return None

    @asynccontextmanager
    async def open_tool(state):
        yield Target()

    await ctx.require(TOOLS).declare_group(ctx, always_on=True)
    await ctx.provide(CHAT_MODELS, Models())
    await ctx.provide(MODEL_CALLS, store.read_call)
    await ctx.provide(MODEL_CALL_STATS, store.read_call_stats)
    await ctx.provide(MODEL_PROJECTION, ProjectionOwner())
    await ctx.provide(MODEL_MESSAGE_CHECKS, MessageChecksOwner())
    await ctx.provide(MODEL_CONTENT, ContentOwner())
    await ctx.provide(MODEL_SELECTION, SelectionOwner())
    await ctx.provide(ServiceKey("fixture.calls"), calls)
'''


@asynccontextmanager
async def application(tmp_path):
    sources = tmp_path / "plugins"
    workspace = tmp_path / "workspace"
    initialize_plugin_workspace(workspace)
    save_config(workspace / "plugin-data/reply-builtin", {"max_steps": 8})
    for name in (
        "ledger", "commands", "sources", "content", "context", "tools",
        "conversation", "react", "turn_projection", "reply_program", "reply", "goal",
    ):
        shutil.copytree(
            Path(__file__).parents[1] / "plugins" / name,
            sources / name,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    provider = sources / "test_provider"
    provider.mkdir()
    (provider / "plugin.py").write_text(_PROVIDER)
    host = PluginManager(
        [sources], workspace=workspace,
        installed_cache_root=tmp_path / "home/cache",
    )
    try:
        await host.load_all()
        yield host
    finally:
        await host.terminate_all()


@pytest.mark.asyncio
async def test_goal_command_runs_until_model_completes(tmp_path):
    async with application(tmp_path) as host:
        root = host.live_root
        assert root is not None
        message = ChannelInboundMessage(
            "test", "user", "room", "/goal ship the feature",
            datetime.now(timezone.utc), {},
        )
        accept = root.context.require(CHANNEL_INPUT)
        accepted = await accept("test:room", "u1", message)
        assert isinstance(accepted.body, Input)

        log = root.context.require(MESSAGE_CATALOG)._log
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            rows = log.reader("test:room").snapshot()
            if any(
                isinstance(row.body, Output) and row.body.finish == "complete"
                and any(
                    isinstance(part, ContentPart) and part.kind == "text"
                    and part.value == "finished"
                    for part in row.body.parts
                )
                for row in rows
            ):
                break
            await asyncio.sleep(0.05)
        rows = log.reader("test:room").snapshot()
        goal_inputs = [row for row in rows if row.author == "goal" and isinstance(row.body, Input)]
        assert goal_inputs, [type(row.body).__name__ for row in rows]
        record = log.owner("plugin:goal").read(goal_key("test:room"))
        assert record is not None
        goal = decode_goal("test:room", record.value)
        assert goal.status.value == "complete"
        assert goal.continuations == 1


@pytest.mark.asyncio
async def test_idle_planning_stands_down_while_a_goal_carries_the_conversation():
    """Two proactive paths must not contact the owner about the same work.

    A running goal is already advancing the reference conversation on its own
    turns, so idle planning reports that and proposes nothing. Once the goal
    stops being active, idle planning is free again.
    """
    from types import SimpleNamespace

    from plugins.akasha.contract import SEMANTIC_INTEREST
    from plugins.drift.producer import Config, Producer
    from plugins.goal.contract import GOAL_STATUS
    from plugins.goal.state import GoalStatus, new_goal
    from plugins.models.contract import MODEL_CATALOG, MODEL_SELECTION, ChatModelSelection
    from plugins.ledger.contract import MESSAGE_CATALOG, OWNER_STATE

    session = "akashic:owner"
    goal = new_goal(session_id=session, goal_id="g1", objective="把展览清单做完")

    def context(current):
        reader = SimpleNamespace(
            deleted=False,
            attributes=SimpleNamespace(visibility="listed"),
            metadata=lambda: {},
        )
        catalog = SimpleNamespace(exists=lambda _s: True, reader=lambda _s: reader)
        models = SimpleNamespace(
            revision=1,
            models=(SimpleNamespace(model_id="m", availability="available"),),
            role_bindings={"default": "m"},
            model=lambda _id: SimpleNamespace(availability="available"),
        )

        class Ctx:
            data_root = None

            def require(self, key):
                if key is OWNER_STATE:
                    # No prior initiative is outstanding, so recovery is a no-op
                    # and run_once reaches the goal check.
                    return SimpleNamespace(open=lambda _ctx: SimpleNamespace(read=lambda _k: None))
                if key is MESSAGE_CATALOG:
                    return catalog
                if key is MODEL_SELECTION:
                    return SimpleNamespace(read_saved=lambda _m: ChatModelSelection())
                if key is MODEL_CATALOG:
                    return SimpleNamespace(snapshot=lambda: models)
                if key is SEMANTIC_INTEREST:
                    return SimpleNamespace(status=lambda: None)
                raise KeyError(key)

            def borrow(self, key):
                from contextlib import contextmanager

                @contextmanager
                def scope():
                    yield (SimpleNamespace(get=_get) if key is GOAL_STATUS else None)

                return scope()

        async def _get(_session):
            return current

        return Ctx()

    store = SimpleNamespace(latest_proposal=lambda: None)

    # An active goal owns the conversation; idle planning reports and stops.
    producer = Producer(context(goal), store, Config(auto_enabled=True, context_session_id=session))
    result = await producer.run_once(manual=False)
    assert result["status"] == "goal_running" and result["goal_id"] == "g1"

    # A goal that has stopped, or none at all, no longer holds idle planning
    # back: it gets past the goal check and goes on to the rest of planning,
    # which this fixture does not stand up.
    async def outcome(current):
        producer = Producer(context(current), store,
                            Config(auto_enabled=True, context_session_id=session))
        try:
            return (await producer.run_once(manual=False)).get("status")
        except AttributeError:
            return "past_goal_gate"

    assert await outcome(replace(goal, status=GoalStatus.COMPLETE)) == "past_goal_gate"
    assert await outcome(replace(goal, status=GoalStatus.PAUSED)) == "past_goal_gate"
    assert await outcome(None) == "past_goal_gate"
