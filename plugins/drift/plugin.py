from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from importlib import import_module
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from agent.plugin_composition import (
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    Context,
    ServiceKey,
)
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from core.common.file_io import run_file_io
from plugins.akasha.contract import MEMORY_QUERIES, SEMANTIC_INTEREST
from plugins.drift.contract import DRIFT_DELIVERY_V2, DRIFT_INITIATIVE, DRIFT_WAKE_V2
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import MESSAGE_CATALOG, OWNER_STATE, Input
from plugins.models.contract import CHAT_MODELS, MODEL_CATALOG, MODEL_SELECTION
from plugins.timer.contract import TIMERS
from plugins.ui.contract import Configuration, ModelSettings, mount_settings

from .producer import Config, Producer, text
from .store import DriftStore

api_version = 3
name = "drift"
version = "3.1.0"
desc = "根据近期交流与历史进行有预算的空闲思考，经原生主动链路研究与分享"
author = "Kirakira Core"
inject = (PLUGIN_CONFIG, MESSAGE_CATALOG, OWNER_STATE)
workspace_roots = ()
workspace_files = ()

SETTINGS = ServiceKey[Configuration]("drift.settings.v1")
FIELDS = ("auto_enabled", "context_session_id", "purpose", "idle_seconds", "interval_seconds", "daily_budget")


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Settings(ModelSettings):
    async def read(self):
        result = await super().read()
        sessions = self.ctx.require(MESSAGE_CATALOG).sessions(visibility="listed", limit=100).items
        result["choices"] = {"context_session_id": [
            {"value": item.session_id, "label": item.title or text(item.first_message)[:60] or "未命名对话"}
            for item in sessions if item.first_message is not None and isinstance(item.first_message.body, Input)
        ]}
        return result


class _AsyncWakeServices:
    def __init__(self, store: DriftStore) -> None:
        self._store = store

    async def snapshot(self, now: datetime) -> Mapping[str, object]:
        return await run_file_io(lambda: self._store.snapshot(now))

    async def select(
        self,
        ref: Mapping[str, object],
        accepted_turn: Mapping[str, object],
        now: datetime,
    ) -> Mapping[str, object]:
        return await run_file_io(lambda: self._store.select(ref, accepted_turn, now))

    async def transition(self, token: str, action: str) -> Mapping[str, object]:
        return await run_file_io(lambda: self._store.transition(token, action))

    async def selection(
        self, accepted_turn: Mapping[str, object]
    ) -> Mapping[str, object] | None:
        return await run_file_io(lambda: self._store.selection(accepted_turn))


class _AsyncDeliveryServices:
    def __init__(self, store: DriftStore) -> None:
        self._store = store

    async def lookup(
        self, accepted_turn: Mapping[str, object]
    ) -> Mapping[str, object] | None:
        return await run_file_io(lambda: self._store.delivery(accepted_turn))

    async def settle(self, selection_token: str, settlement_ref: str) -> Mapping[str, object]:
        return await run_file_io(lambda: self._store.settle_delivery(selection_token, settlement_ref))


async def apply(ctx: Context) -> None:
    """Publish the narrow Drift view over one generation-scoped store."""

    store = DriftStore(ctx.data_root / "drift.sqlite3")
    await run_file_io(store.initialize)
    _ = await ctx.provide(DRIFT_WAKE_V2, _AsyncWakeServices(store))
    _ = await ctx.provide(DRIFT_DELIVERY_V2, _AsyncDeliveryServices(store))
    config = Config.model_validate(ctx.config)
    available = [False]

    async def function(child):
        producer = Producer(child, store, config)
        def readiness():
            available[0] = True
            return lambda: available.__setitem__(0, False)
        await child.effect(readiness, label="initiative.availability")
        api = SimpleNamespace(run_once=child.entrypoint(producer.run_once), status=child.entrypoint(producer.status))
        await child.provide(DRIFT_INITIATIVE, api)

        async def run(_request):
            return await api.run_once(manual=True)

        async def status(_request):
            return await api.status()

        for method, invoke in (("run", run), ("status", status)):
            await child.provide(RpcMethod.key("drift/" + method), RpcMethod(Empty, child.entrypoint(invoke)))
        watcher = None

        async def start(_event):
            nonlocal watcher
            watcher = await child.spawn(producer.follow(), name="drift.reflect")

        async def stop(_event):
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass

        await child.on(RUNTIME_STARTED, start)
        await child.on(RUNTIME_STOPPING, stop)

    await ctx.inject((CHAT_MODELS, MODEL_CATALOG, MODEL_SELECTION, MESSAGE_CATALOG, OWNER_STATE,
                      MEMORY_QUERIES, SEMANTIC_INTEREST, TIMERS), function, name="initiative")

    async def status():
        return {"ready": available[0], "blocked": not available[0],
                "reason": "" if available[0] else "需要可用的聊天模型和记忆服务；准备好后才开始思考。"}

    await mount_settings(ctx, SETTINGS, Settings(ctx, Config, FIELDS, status=status), lambda: import_module(".dashboard", __package__))
