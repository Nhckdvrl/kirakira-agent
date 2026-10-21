"""Long-term follow-ups use the same event, investigation and delivery runtime."""

import asyncio
from importlib import import_module
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import RUNTIME_STARTED, RUNTIME_STOPPING
from plugins.akasha.contract import AKASHA_RECORDS_VIEW
from plugins.delivery.contract import DELIVERY_READ
from plugins.eventmail.contract import EVENTMAIL_CONTENT_SOURCE
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, OWNER_STATE
from plugins.ui.contract import UI

from .contract import FOLLOWUPS, Feedback, SetEnabled, WatchId, WatchSpec
from .service import Service

api_version = 3
name = "followups"
version = "1.0.0"
desc = "从真实对话建立信息跟进，调查后分享或跳过，并查看依据与反馈"
inject = (
    OWNER_STATE,
    MESSAGE_CATALOG,
    BINDINGS,
    EVENTMAIL_CONTENT_SOURCE,
    DELIVERY_READ,
    AKASHA_RECORDS_VIEW,
)


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=512)


async def apply(ctx):
    service = Service(
        ctx,
        ctx.require(OWNER_STATE).open(ctx),
        ctx.require(MESSAGE_CATALOG),
        ctx.require(EVENTMAIL_CONTENT_SOURCE),
        ctx.require(DELIVERY_READ),
        ctx.require(BINDINGS),
        ctx.require(AKASHA_RECORDS_VIEW),
    )
    await ctx.effect(lambda: service.http.aclose, label="followups.http")
    api = SimpleNamespace(
        **{
            method: ctx.entrypoint(getattr(service, method))
            for method in (
                "overview",
                "create",
                "poll",
                "timeline",
                "set_enabled",
                "feedback",
            )
        }
    )
    await ctx.provide(FOLLOWUPS, api)
    for method, params, call in (
        ("overview", Session, lambda request: api.overview(request.session_id)),
        ("create", WatchSpec, api.create),
        ("poll", WatchId, lambda request: api.poll(request.watch_id)),
        ("timeline", WatchId, lambda request: api.timeline(request.watch_id)),
        ("set-enabled", SetEnabled, api.set_enabled),
        ("feedback", Feedback, api.feedback),
    ):

        async def invoke(request, call=call):
            return await call(request)

        await ctx.provide(
            RpcMethod.key("followups/" + method),
            RpcMethod(params, ctx.entrypoint(invoke)),
        )

    async def register_ui(child):
        await child.require(UI).register(
            child,
            web="web_module.js",
            dashboard=lambda: import_module(".dashboard", __package__),
            requires=("conversation.tools.v2",),
            contract_digests={
                "conversation.tools.v2": "dcbf5db629d4fa6fed4019ad91663f9af94c5b9a02f9e67c94b0745e40f19049"
            },
        )

    await ctx.inject((UI, FOLLOWUPS), register_ui, name="web")

    watcher = None

    async def start(_event):
        nonlocal watcher
        watcher = await ctx.spawn(service.follow(), name="followups.poll")

    async def stop(_event):
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    await ctx.on(RUNTIME_STARTED, start)
    await ctx.on(RUNTIME_STOPPING, stop)
