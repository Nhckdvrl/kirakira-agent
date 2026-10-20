"""Controlled historical inputs through real admission and writer capabilities."""

from pydantic import BaseModel, Field

from plugins.content.contract import CONTENT
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import (
    MESSAGE_WRITERS,
    SESSION_ADMISSION,
    ContentPart,
    Input,
    Output,
    SessionAttributes,
)

api_version = 3
name = "alignment-fixture"
version = "1.0.0"
inject = (CONTENT, MESSAGE_WRITERS, SESSION_ADMISSION)


class Seed(BaseModel):
    session_id: str
    records: list[dict] = Field(default_factory=list, max_length=8)


async def apply(ctx):
    @ctx.entrypoint
    async def seed(params):
        await ctx.require(SESSION_ADMISSION).ensure_async(
            ctx, params.session_id, SessionAttributes(learning="excluded")
        )
        for index, record in enumerate(params.records):
            body = (
                Input((ContentPart("text", record["text"]),))
                if record["kind"] == "input"
                else Output(
                    (ContentPart("text", record["text"]),),
                    record.get("finish", "complete"),
                )
            )
            writer = ctx.require(MESSAGE_WRITERS).bind(
                ctx,
                author="evaluation-fixture",
                source=record.get("source", "evaluation-history"),
                body_types=(type(body),),
                content={"text": ctx.require(CONTENT).check_text},
            )(params.session_id)
            try:
                await writer.append_async(f"{params.session_id}:seed:{index}", body)
            finally:
                writer.expire()
        return {"seeded": len(params.records), "learning": "excluded"}

    await ctx.provide(RpcMethod.key("evaluation/seed"), RpcMethod(Seed, seed))
