"""Compose configured channel adapters; no CLI or agent construction."""

import logging
import os
from pathlib import Path

from agent.config import config_value
from bootstrap.environment import env_bool, env_list
from bus.event_bus import EventBus
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.host import ChannelHost
from infra.channels.qq_channel import QQChannel
from infra.channels.qqbot_channel import QQBotChannel
from infra.channels.telegram_channel import TelegramChannel
from infra.channels.web_chat_channel import WebChannel
from session.manager import SessionManager


def build_channel_host(
    *,
    workdir: Path,
    bus: MessageBus,
    event_bus: EventBus,
    session_manager: SessionManager,
    enable_web: bool = False,
    enable_telegram: bool = False,
    enable_qq: bool = False,
    enable_qqbot: bool = False,
    interrupt=None,
    memory=None,
    app_config=None,
    dashboard=None,
    push_tool=None,
) -> ChannelHost | None:
    from agent.looping.interrupt import InterruptController
    from agent.tools.message_push import MessagePushTool
    from core.net.http import SharedHttpResources
    from infra.channels.base import AttachmentStore

    app_config = app_config or {}
    push_tool = push_tool or MessagePushTool(chat_lane=bus.chat_lane)
    interrupt_controller = InterruptController(interrupt) if interrupt else None
    attachment_store = AttachmentStore(workdir / "uploads")
    http_resources = SharedHttpResources()
    host = ChannelHost(
        lambda channel: ChannelContext(
            bus=bus,
            session_manager=session_manager,
            event_bus=event_bus,
            workspace=workdir,
            log=logging.getLogger("channels.%s" % channel.name),
            interrupt=interrupt,
            memory=memory,
            push_tool=push_tool,
            attachment_store=attachment_store,
            http_resources=http_resources,
            interrupt_controller=interrupt_controller,
        )
    )
    added = False
    chat_config = config_value(app_config, "channels", "chat", default={}) or {}
    if enable_web or env_bool(
        "KIRAKIRA_WEB_ENABLED", bool(chat_config.get("enabled", False))
    ):
        host.add(
            WebChannel(
                host=os.getenv(
                    "KIRAKIRA_WEB_HOST", str(chat_config.get("host") or "127.0.0.1")
                ),
                port=int(
                    os.getenv("KIRAKIRA_WEB_PORT", str(chat_config.get("port") or 6322))
                ),
                channel_name=os.getenv(
                    "KIRAKIRA_WEB_CHANNEL",
                    str(chat_config.get("channel_name") or "web"),
                ),
                dashboard=dashboard,
            )
        )
        added = True
    telegram_config = config_value(app_config, "channels", "telegram", default={}) or {}
    telegram_token = (
        os.getenv("TELEGRAM_BOT_TOKEN") or str(telegram_config.get("token") or "")
    ).strip()
    if enable_telegram or env_bool(
        "KIRAKIRA_TELEGRAM_ENABLED",
        bool(telegram_config.get("enabled", bool(telegram_token))),
    ):
        if not telegram_token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is required when Telegram channel is enabled"
            )
        host.add(
            TelegramChannel(
                token=telegram_token,
                bus=bus,
                session_manager=session_manager,
                allow_from=env_list("TELEGRAM_ALLOW_FROM")
                or [str(item) for item in telegram_config.get("allow_from", [])],
                event_bus=event_bus,
                interrupt_controller=interrupt_controller,
                channel_name=os.getenv(
                    "KIRAKIRA_TELEGRAM_CHANNEL",
                    str(telegram_config.get("channel_name") or "telegram"),
                ),
            )
        )
        added = True
    qq_config = config_value(app_config, "channels", "qq", default={}) or {}
    qq_groups = qq_config.get("groups") or []
    configured_group_ids = [
        str(item.get("group_id"))
        for item in qq_groups
        if isinstance(item, dict) and item.get("group_id")
    ]
    group_policies = {
        str(item["group_id"]): {
            "allow_from": [str(user) for user in item.get("allow_from", [])],
            "require_at": bool(item.get("require_at", True)),
        }
        for item in qq_groups
        if isinstance(item, dict) and item.get("group_id")
    }
    if enable_qq or env_bool(
        "KIRAKIRA_QQ_ENABLED",
        bool(qq_config.get("enabled", bool(qq_config.get("bot_uin")))),
    ):
        host.add(
            QQChannel(
                bot_uin=os.getenv("QQ_BOT_UIN", str(qq_config.get("bot_uin") or "")),
                api_base_url=os.getenv(
                    "ONEBOT_API_BASE_URL",
                    str(qq_config.get("api_base_url") or "http://127.0.0.1:3000"),
                ),
                webhook_host=os.getenv("KIRAKIRA_QQ_WEBHOOK_HOST", "127.0.0.1"),
                webhook_port=int(os.getenv("KIRAKIRA_QQ_WEBHOOK_PORT", "8766")),
                access_token=os.getenv(
                    "ONEBOT_ACCESS_TOKEN", str(qq_config.get("access_token") or "")
                ),
                allow_from=env_list("QQ_ALLOW_FROM")
                or [str(item) for item in qq_config.get("allow_from", [])],
                group_allow=env_list("QQ_GROUP_ALLOW") or configured_group_ids,
                group_policies=group_policies,
                require_at=env_bool(
                    "QQ_REQUIRE_AT", bool(qq_config.get("require_at", True))
                ),
                channel_name=os.getenv(
                    "KIRAKIRA_QQ_CHANNEL", str(qq_config.get("channel_name") or "qq")
                ),
            )
        )
        added = True
    qqbot_config = config_value(app_config, "channels", "qqbot", default={}) or {}
    qqbot_app_id = (
        os.getenv("QQBOT_APP_ID") or str(qqbot_config.get("app_id") or "")
    ).strip()
    qqbot_secret = (
        os.getenv("QQBOT_CLIENT_SECRET") or str(qqbot_config.get("client_secret") or "")
    ).strip()
    if enable_qqbot or env_bool(
        "KIRAKIRA_QQBOT_ENABLED",
        bool(qqbot_config.get("enabled", bool(qqbot_app_id and qqbot_secret))),
    ):
        if not qqbot_app_id or not qqbot_secret:
            raise RuntimeError(
                "QQBOT_APP_ID and QQBOT_CLIENT_SECRET are required when official QQBot is enabled"
            )
        host.add(
            QQBotChannel(
                app_id=qqbot_app_id,
                client_secret=qqbot_secret,
                allow_from=env_list("QQBOT_ALLOW_FROM")
                or [str(item) for item in qqbot_config.get("allow_from", [])],
                channel_name=os.getenv(
                    "KIRAKIRA_QQBOT_CHANNEL",
                    str(qqbot_config.get("channel_name") or "qqbot"),
                ),
                api_base_url=str(
                    qqbot_config.get("api_base_url") or "https://api.sgroup.qq.com"
                ),
            )
        )
        added = True
    return host if added else None
