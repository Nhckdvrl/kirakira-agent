"""Authenticated Gateway ingress into the three native EventMail source ports."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from plugins.eventmail.contract import (
    EVENTMAIL_ALERT_SOURCE,
    EVENTMAIL_CONTENT_SOURCE,
    EVENTMAIL_CONTEXT_SOURCE,
)
from plugins.gateway.contract import RpcMethod

api_version = 3
name = "event-ingress"
version = "1.0.0"
desc = "Local Content/Alert/Context events through the native authenticated Gateway"
inject = (EVENTMAIL_CONTENT_SOURCE, EVENTMAIL_ALERT_SOURCE, EVENTMAIL_CONTEXT_SOURCE)


class ContentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str = Field(min_length=1, max_length=256)
    items: list[dict] = Field(min_length=1, max_length=100)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=256)
    payload: dict
    observed_at: AwareDatetime
    expires_at: AwareDatetime | None = None


class Ack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settlement_ref: str = Field(min_length=1)


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def apply(ctx):
    # Each source ID belongs to this plugin's actual lease. Caller-controlled
    # IDs cannot impersonate another producer, and no second event database exists.
    content = ctx.require(EVENTMAIL_CONTENT_SOURCE).bind("kirakira.local-content")
    alert = ctx.require(EVENTMAIL_ALERT_SOURCE).bind("kirakira.local-alert")
    context = ctx.require(EVENTMAIL_CONTEXT_SOURCE).bind("kirakira.local-context")
    for kind, source in (("content", content), ("alert", alert), ("context", context)):
        await ctx.effect(
            lambda source=source: source.close, label="event-ingress.source:" + kind
        )

    async def submit(params):
        return await content.submit(params.batch_id, params.items)

    async def report_alert(params):
        return await alert.report(**params.model_dump())

    async def report_context(params):
        return await context.report(**params.model_dump())

    async def unsettled(_params):
        return {"items": list(await content.unsettled())}

    async def ack(params):
        return await content.ack(params.settlement_ref)

    for method, params, call in (
        ("content", ContentBatch, submit),
        ("alert", Event, report_alert),
        ("context", Event, report_context),
        ("unsettled", Empty, unsettled),
        ("ack", Ack, ack),
    ):
        await ctx.provide(
            RpcMethod.key("event-ingress/" + method),
            RpcMethod(params, ctx.entrypoint(call)),
        )
