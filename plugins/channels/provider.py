from agent.composition.lifecycle import on_close, on_start

"""Declare this owner's dependencies, capabilities and resource lifetime."""

import logging
import os

from agent.config import config_value
from bus.contract import BUS, EVENTS
from infra.channels.contract import ChannelContext
from infra.channels.host import ChannelHost
from plugins.channels.contract import CHANNELS
from plugins.channels.factory import build_channel_host
from plugins.dashboard.contract import DASHBOARD
from plugins.default_memory.contract import MEMORY
from plugins.extensions.contract import EXTENSIONS
from plugins.ledger.contract import SESSIONS
from plugins.proactive_flow.contract import PROACTIVE
from plugins.reply.contract import FOLLOWER
from plugins.tools.contract import PUSH

inject = (
    BUS,
    EVENTS,
    SESSIONS,
    FOLLOWER,
    MEMORY,
    DASHBOARD,
    PUSH,
    EXTENSIONS,
    PROACTIVE,
)
provides = (CHANNELS,)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    app_config = ctx.config["app_config"]
    bus = ctx.get(BUS)
    event_bus = ctx.get(EVENTS)
    session_manager = ctx.get(SESSIONS)
    loop = ctx.get(FOLLOWER)
    memory = ctx.get(MEMORY)
    dashboard = ctx.get(DASHBOARD)
    push_tool = ctx.get(PUSH)
    plugin_manager = ctx.get(EXTENSIONS)
    proactive_loop = ctx.get(PROACTIVE)
    enable_web, enable_telegram, enable_qq, enable_qqbot = ctx.config["channels"]
    proactive_enabled = bool(
        config_value(app_config, "proactive", "enabled", default=False)
    )
    proactive_channel = str(
        config_value(app_config, "proactive", "target", "channel", default="") or ""
    ).strip()
    if proactive_enabled:
        web_cfg = config_value(app_config, "channels", "chat", default={}) or {}
        telegram_cfg = (
            config_value(app_config, "channels", "telegram", default={}) or {}
        )
        qq_cfg = config_value(app_config, "channels", "qq", default={}) or {}
        qqbot_cfg = config_value(app_config, "channels", "qqbot", default={}) or {}
        web_name = os.getenv(
            "KIRAKIRA_WEB_CHANNEL", str(web_cfg.get("channel_name") or "web")
        )
        telegram_name = os.getenv(
            "KIRAKIRA_TELEGRAM_CHANNEL",
            str(telegram_cfg.get("channel_name") or "telegram"),
        )
        qq_name = os.getenv(
            "KIRAKIRA_QQ_CHANNEL", str(qq_cfg.get("channel_name") or "qq")
        )
        qqbot_name = os.getenv(
            "KIRAKIRA_QQBOT_CHANNEL",
            str(qqbot_cfg.get("channel_name") or "qqbot"),
        )
        enable_web = enable_web or proactive_channel == web_name
        enable_telegram = enable_telegram or proactive_channel == telegram_name
        enable_qq = enable_qq or proactive_channel == qq_name
        enable_qqbot = enable_qqbot or proactive_channel == qqbot_name
    channel_host = build_channel_host(
        workdir=workdir,
        bus=bus,
        event_bus=event_bus,
        session_manager=session_manager,
        enable_web=enable_web,
        enable_telegram=enable_telegram,
        enable_qq=enable_qq,
        enable_qqbot=enable_qqbot,
        interrupt=loop.request_interrupt,
        memory=memory,
        app_config=app_config,
        dashboard=dashboard,
        push_tool=push_tool,
    )
    if channel_host is not None:
        await on_close(ctx, lambda: channel_host.stop_all())
    if plugin_manager.channels:
        if channel_host is None:
            channel_host = ChannelHost(
                lambda channel: ChannelContext(
                    bus=bus,
                    session_manager=session_manager,
                    event_bus=event_bus,
                    workspace=workdir,
                    log=logging.getLogger("channels.%s" % channel.name),
                    interrupt=loop.request_interrupt,
                    memory=memory,
                    push_tool=push_tool,
                )
            )
        for plugin_channel in plugin_manager.channels:
            channel_host.add(plugin_channel)
    if proactive_loop is not None:
        available_channels = {
            channel.name for channel in (channel_host.channels if channel_host else [])
        }
        target_channel = proactive_loop.target_channel
        if target_channel not in available_channels:
            raise RuntimeError(
                "proactive target channel %r is not configured; available channels: %s"
                % (target_channel, ", ".join(sorted(available_channels)) or "(none)")
            )

    await ctx.provide(CHANNELS, channel_host)

    async def start_channels():
        if channel_host is not None and ctx.config["_host"].runtime_options.get(
            "start_channels", True
        ):
            await channel_host.start_all()

    await on_start(ctx, start_channels)
