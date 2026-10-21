"""Long-term follow-ups use the same event, investigation and delivery runtime."""

import asyncio
from importlib import import_module
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import CREDENTIALS, RUNTIME_STARTED, RUNTIME_STOPPING
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from plugins.akasha.contract import AKASHA_RECORDS_VIEW
from plugins.delivery.contract import DELIVERY_READ
from plugins.eventmail.contract import EVENTMAIL_CONTENT_SOURCE
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import (
    BINDINGS,
    MESSAGE_CATALOG,
    MESSAGE_WRITERS,
    OWNER_STATE,
    ContentReferences,
    Input,
)
from plugins.ui.contract import UI

from .contract import (
    FOLLOWUPS,
    Config,
    Feedback,
    Recheck,
    Revise,
    SetEnabled,
    WatchId,
    WatchSpec,
    check_policy_record,
)
from .mail_settings import MAIL_SETTINGS, Mailboxes, Settings
from .service import Service

api_version = 3
name = "followups"
version = "1.1.0"
desc = "从真实对话建立信息跟进，调查后分享或跳过，并查看依据与反馈"
inject = (
    OWNER_STATE,
    MESSAGE_CATALOG,
    BINDINGS,
    EVENTMAIL_CONTENT_SOURCE,
    DELIVERY_READ,
    AKASHA_RECORDS_VIEW,
    CREDENTIALS,
    PLUGIN_CONFIG,
    MESSAGE_WRITERS,
)


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=512)


async def apply(ctx):
    config = Config.model_validate(ctx.config)

    def check_policy(part):
        if not isinstance(part.value, str) or not 1 <= len(part.value) <= 1000:
            raise ValueError("关注规则必须是有界的原始用户文字")
        return ContentReferences()

    writers = ctx.require(MESSAGE_WRITERS).bind(
        ctx,
        author="user",
        source="followups",
        body_types=(Input,),
        content={"text": check_policy, "followups.policy": check_policy_record},
    )
    service = Service(
        ctx,
        ctx.require(OWNER_STATE).open(ctx),
        ctx.require(MESSAGE_CATALOG),
        ctx.require(EVENTMAIL_CONTENT_SOURCE),
        ctx.require(DELIVERY_READ),
        ctx.require(BINDINGS),
        ctx.require(AKASHA_RECORDS_VIEW),
        mailbox=Mailboxes(ctx, config.mailbox),
        writers=writers,
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
                "revise",
                "reset_mailbox",
                "recheck",
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
        ("revise", Revise, api.revise),
        ("recheck", Recheck, api.recheck),
        ("reset-mailbox", WatchId, lambda request: api.reset_mailbox(request.watch_id)),
    ):

        async def invoke(request, call=call):
            return await call(request)

        await ctx.provide(
            RpcMethod.key("followups/" + method),
            RpcMethod(params, ctx.entrypoint(invoke)),
        )

    settings = Settings(ctx)
    await ctx.provide(
        MAIL_SETTINGS,
        SimpleNamespace(
            read=ctx.entrypoint(settings.read),
            save=ctx.entrypoint(settings.save),
            receipt=ctx.entrypoint(settings.receipt),
        ),
    )

    async def register_ui(child):
        await child.require(UI).register(
            child,
            web="web_module.js",
            dashboard=lambda: import_module(".dashboard", __package__),
            requires=("conversation.tools.v2", "shell.settings-plugins.v1"),
            contract_digests={
                "conversation.tools.v2": "dcbf5db629d4fa6fed4019ad91663f9af94c5b9a02f9e67c94b0745e40f19049",
                "shell.settings-plugins.v1": "1823b19c778297893495ca9193d688e24c99d079effbaf5ecf0d1148ae0a1aa2",
            },
        )

    await ctx.inject((UI, FOLLOWUPS, MAIL_SETTINGS), register_ui, name="web")

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
