"""固定配置的 Telegram 出站；不创建 Bot 收件实例。"""
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Protocol, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.plugin_composition import (
    CREDENTIALS,
    Context,
    CredentialRef,
)
from plugins.ledger.contract import ARTIFACT_READ
from plugins.ledger.contract import MESSAGE_CATALOG
from plugins.telegram_channel.contract import DRAFT_DELIVERY
from plugins.delivery.contract import (
    DELIVERY_SENDERS as DELIVERY_SENDERS,
)

api_version = 3
name = "telegram_sender"
version = "1.0.0"
desc = "用固定凭据发送 Telegram 正文和附件"


class SenderTarget(Protocol):
    idempotent: bool


function_inject = (DELIVERY_SENDERS, CREDENTIALS, MESSAGE_CATALOG, ARTIFACT_READ)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool | None = False
    token: CredentialRef | None = None
    channel: str = Field(default="telegram", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    api_base: str = "https://api.telegram.org"
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check(self) -> Self:
        url = urlsplit(self.api_base)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("api_base 必须是无凭据的 HTTP(S) URL")
        if self.enabled and self.token is None:
            raise ValueError("启用 Telegram Sender 需要 token")
        return self


async def run(ctx: Context) -> None:
    config = Config.model_validate(ctx.config)
    if not config.enabled:
        return
    token_ref = config.token
    assert token_ref is not None
    # aiohttp 与 Telegram 渲染依赖只在启用后加载。
    import aiohttp

    from .sender import TelegramSender

    # One lazy pool per actual plugin generation. Credentials and send receipts
    # remain per invocation; ordinary replies can reuse DNS/TCP/TLS connections.
    client: aiohttp.ClientSession | None = None

    async def close_http():
        if client is not None:
            await client.close()

    await ctx.effect(lambda: close_http, label="telegram-http")

    @asynccontextmanager
    async def open_sender() -> AsyncGenerator[TelegramSender]:
        nonlocal client
        with ctx.borrow(DRAFT_DELIVERY) as drafts:
            async with ctx.require(CREDENTIALS).open(ctx, {"token": token_ref}) as credentials:
                if client is None:
                    client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.timeout_seconds), trust_env=True)
                yield TelegramSender(client, config.api_base, credentials.credential(token_ref),
                                     ctx.require(MESSAGE_CATALOG), ctx.require(ARTIFACT_READ),
                                     draft_delivery=None if drafts is None else drafts.delivering)

    _ = await ctx.require(DELIVERY_SENDERS).register(ctx, name=config.channel, idempotent=False, open=open_sender)


from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from agent.plugin_composition.runtime_catalog import RUNTIME_CATALOG

inject = (PLUGIN_CONFIG, RUNTIME_CATALOG, CREDENTIALS)


async def apply(ctx: Context) -> None:
    """设置入口常驻，业务依赖只影响功能分支。"""
    from .settings import mount
    function = await ctx.inject(function_inject, run, name="function")
    await mount(ctx, Config, function)
