"""Kirakira Agent CLI and local channel runtime."""

import asyncio
import argparse
import os
import logging
from pathlib import Path
from typing import List

from kirakira_agent.agent import Agent, DEFAULT_SYSTEM
from kirakira_agent.config import load_dotenv, require_env
from kirakira_agent.bus import MessageBus
from kirakira_agent.channels.contract import ChannelContext
from kirakira_agent.channels.host import ChannelHost
from kirakira_agent.channels.qq import QQChannel
from kirakira_agent.channels.telegram import TelegramChannel
from kirakira_agent.channels.web import WebChannel
from kirakira_agent.context_builder import ContextBuilder
from kirakira_agent.event_bus import EventBus
from kirakira_agent.events import InboundMessage, OutboundMessage
from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.models import OpenAICompatibleClient
from kirakira_agent.plugins import PluginManager
from kirakira_agent.runtime import (
    AgentLoop,
    CoreRuntime,
    DefaultReasoner,
    PassiveTurnPipeline,
    RuntimeConfig,
)
from kirakira_agent.schema import JsonDict
from kirakira_agent.session import SessionManager
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools import build_default_registry


def build_agent(workdir: Path) -> Agent:
    load_dotenv(workdir / ".env")
    model = require_env("MODEL_ID")
    client = OpenAICompatibleClient()
    registry = build_default_registry(workdir)
    skills = SkillLoader(workdir / "skills")
    system = (
        DEFAULT_SYSTEM
        + "\nCurrent workspace: %s\nAvailable skills:\n%s" % (workdir, skills.descriptions())
    )
    return Agent(client, registry, model=model, workdir=workdir, system=system)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "enabled")


def _env_list(name: str) -> List[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_channel_host(
    *,
    workdir: Path,
    bus: MessageBus,
    event_bus: EventBus,
    session_manager: SessionManager,
    enable_web: bool = False,
    enable_telegram: bool = False,
    enable_qq: bool = False,
) -> ChannelHost | None:
    host = ChannelHost(
        lambda channel: ChannelContext(
            bus=bus,
            session_manager=session_manager,
            event_bus=event_bus,
            workspace=workdir,
            log=logging.getLogger("channels.%s" % channel.name),
        )
    )
    added = False
    if enable_web or _env_bool("KIRAKIRA_WEB_ENABLED"):
        host.add(
            WebChannel(
                host=os.getenv("KIRAKIRA_WEB_HOST", "127.0.0.1"),
                port=int(os.getenv("KIRAKIRA_WEB_PORT", "8765")),
                channel_name=os.getenv("KIRAKIRA_WEB_CHANNEL", "web"),
            )
        )
        added = True
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if enable_telegram or _env_bool("KIRAKIRA_TELEGRAM_ENABLED"):
        if not telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required when Telegram channel is enabled")
        host.add(
            TelegramChannel(
                token=telegram_token,
                allow_from=_env_list("TELEGRAM_ALLOW_FROM"),
                channel_name=os.getenv("KIRAKIRA_TELEGRAM_CHANNEL", "telegram"),
            )
        )
        added = True
    if enable_qq or _env_bool("KIRAKIRA_QQ_ENABLED"):
        host.add(
            QQChannel(
                bot_uin=os.getenv("QQ_BOT_UIN", ""),
                api_base_url=os.getenv("ONEBOT_API_BASE_URL", "http://127.0.0.1:3000"),
                webhook_host=os.getenv("KIRAKIRA_QQ_WEBHOOK_HOST", "127.0.0.1"),
                webhook_port=int(os.getenv("KIRAKIRA_QQ_WEBHOOK_PORT", "8766")),
                access_token=os.getenv("ONEBOT_ACCESS_TOKEN", ""),
                allow_from=_env_list("QQ_ALLOW_FROM"),
                group_allow=_env_list("QQ_GROUP_ALLOW"),
                require_at=_env_bool("QQ_REQUIRE_AT", True),
                channel_name=os.getenv("KIRAKIRA_QQ_CHANNEL", "qq"),
            )
        )
        added = True
    return host if added else None


async def build_runtime(
    workdir: Path,
    *,
    enable_web: bool = False,
    enable_telegram: bool = False,
    enable_qq: bool = False,
) -> CoreRuntime:
    load_dotenv(workdir / ".env")
    model = require_env("MODEL_ID")
    client = OpenAICompatibleClient()
    bus = MessageBus()
    event_bus = EventBus()
    session_manager = SessionManager(workdir)
    memory = MemoryRuntime(workdir, session_manager=session_manager)
    registry = build_default_registry(workdir, memory=memory, session_manager=session_manager, bus=bus)
    context = ContextBuilder(workdir, memory)
    config = RuntimeConfig(
        model=model,
        max_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "10")),
        max_tokens=int(os.getenv("AGENT_MAX_TOKENS", "8192")),
        history_window=int(os.getenv("AGENT_HISTORY_WINDOW", "40")),
    )
    reasoner = DefaultReasoner(
        model_client=client,
        tools=registry,
        config=config,
        context=context,
        event_bus=event_bus,
    )
    pipeline = PassiveTurnPipeline(
        bus=bus,
        event_bus=event_bus,
        session_manager=session_manager,
        memory=memory,
        tools=registry,
        reasoner=reasoner,
        config=config,
    )
    loop = AgentLoop(bus=bus, pipeline=pipeline)
    plugin_manager = PluginManager(
        [workdir / "plugins"],
        event_bus=event_bus,
        tool_registry=registry,
        workspace=workdir,
        session_manager=session_manager,
        memory=memory,
    )
    await plugin_manager.load_all()
    reasoner.add_tool_hooks(plugin_manager.tool_hooks)
    reasoner.add_prompt_render_plugin_modules(plugin_manager.prompt_render_modules)
    reasoner.add_before_step_plugin_modules(plugin_manager.before_step_modules)
    reasoner.add_after_step_plugin_modules(plugin_manager.after_step_modules)
    pipeline.add_before_turn_plugin_modules(plugin_manager.before_turn_modules)
    pipeline.add_before_reasoning_plugin_modules(plugin_manager.before_reasoning_modules)
    pipeline.add_after_reasoning_plugin_modules(plugin_manager.after_reasoning_modules)
    pipeline.add_after_turn_plugin_modules(plugin_manager.after_turn_modules)
    channel_host = _build_channel_host(
        workdir=workdir,
        bus=bus,
        event_bus=event_bus,
        session_manager=session_manager,
        enable_web=enable_web,
        enable_telegram=enable_telegram,
        enable_qq=enable_qq,
    )
    return CoreRuntime(
        bus=bus,
        event_bus=event_bus,
        session_manager=session_manager,
        memory=memory,
        tools=registry,
        context=context,
        reasoner=reasoner,
        pipeline=pipeline,
        loop=loop,
        channel_host=channel_host,
    )


def print_response_text(response_text: str) -> None:
    if response_text:
        print(response_text)


def repl(agent: Agent, workdir: Path) -> None:
    history: List[JsonDict] = []
    skill_loader = SkillLoader(workdir / "skills")
    print("kirakira-agent ready. /tools /skills /compact /exit")
    while True:
        try:
            query = input("kirakira >> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query in ("/exit", "exit", "q", "quit"):
            break
        if query == "/tools":
            print("\n".join(agent.tool_registry.names()))
            continue
        if query == "/skills":
            skill_loader.reload()
            print(skill_loader.descriptions())
            continue
        if query == "/compact":
            if history:
                history[:] = agent.compact(history)
                print("Context compacted.")
            else:
                print("No context to compact.")
            continue

        history.append({"role": "user", "content": query})
        try:
            response = agent.run(history)
        except RuntimeError as exc:
            print("Error: %s" % exc)
            continue
        print_response_text(response.text)


async def runtime_repl(runtime: CoreRuntime, workdir: Path) -> None:
    queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def on_outbound(msg: OutboundMessage) -> None:
        await queue.put(msg)

    runtime.bus.subscribe_outbound("cli", on_outbound)
    loop_task = asyncio.create_task(runtime.loop.run(), name="agent_loop")
    dispatch_task = asyncio.create_task(runtime.bus.dispatch_outbound(), name="bus_dispatch")
    print("kirakira-agent ready. /tools /skills /memory /exit")
    try:
        while True:
            query = await asyncio.to_thread(input, "kirakira >> ")
            query = query.strip()
            if not query:
                continue
            if query in ("/exit", "exit", "q", "quit"):
                break
            if query == "/tools":
                print("\n".join(runtime.tools.names()))
                continue
            if query == "/skills":
                runtime.context.skills.reload()
                print(runtime.context.skills.descriptions())
                continue
            if query == "/memory":
                print(runtime.memory.store.read_long_term())
                continue
            await runtime.bus.publish_inbound(
                InboundMessage(
                    channel="cli",
                    sender="local",
                    chat_id="local",
                    content=query,
                )
            )
            outbound = await queue.get()
            print_response_text(outbound.content)
    finally:
        runtime.loop.stop()
        runtime.bus.stop()
        loop_task.cancel()
        dispatch_task.cancel()
        await asyncio.gather(loop_task, dispatch_task, return_exceptions=True)


async def runtime_serve(runtime: CoreRuntime) -> None:
    tasks = await runtime.start_background()
    try:
        print("kirakira-agent server running. Ctrl+C to stop.")
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runtime.stop_background(tasks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kirakira Agent")
    parser.add_argument("--serve", action="store_true", help="Run background agent loop and configured channels.")
    parser.add_argument("--web", action="store_true", help="Enable stdlib web channel.")
    parser.add_argument("--telegram", action="store_true", help="Enable Telegram Bot API channel.")
    parser.add_argument("--qq", action="store_true", help="Enable QQ OneBot webhook channel.")
    args = parser.parse_args()
    workdir = Path(os.getcwd()).resolve()
    runtime = asyncio.run(
        build_runtime(
            workdir,
            enable_web=args.web,
            enable_telegram=args.telegram,
            enable_qq=args.qq,
        )
    )
    if args.serve or args.web or args.telegram or args.qq:
        asyncio.run(runtime_serve(runtime))
    else:
        asyncio.run(runtime_repl(runtime, workdir))
