"""Kirakira Agent CLI and local channel runtime."""

import asyncio
import argparse
import os
import logging
from pathlib import Path
from typing import List

from kirakira_agent.agent import Agent, DEFAULT_SYSTEM
from kirakira_agent.config import config_value, load_dotenv, load_toml_config, require_env
from kirakira_agent.bus import MessageBus
from kirakira_agent.channels.contract import ChannelContext
from kirakira_agent.channels.host import ChannelHost
from kirakira_agent.channels.qq import QQChannel
from kirakira_agent.channels.telegram import TelegramChannel
from kirakira_agent.channels.web import WebChannel
from kirakira_agent.context_builder import ContextBuilder
from kirakira_agent.context_policy import recommended_context_settings
from kirakira_agent.event_bus import EventBus
from kirakira_agent.events import InboundMessage, OutboundMessage
from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.mcp import McpCatalogPublisher, WorkspaceMcpWatcher
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
from kirakira_agent.snapshot import RuntimeSnapshotStore
from kirakira_agent.scheduler import SchedulerService
from kirakira_agent.subagent import SubagentManager
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools import build_default_registry


def build_agent(workdir: Path) -> Agent:
    load_dotenv(workdir / ".env")
    app_config = load_toml_config(workdir / "config.toml")
    model = os.getenv("MODEL_ID") or str(
        config_value(app_config, "llm", "main", "model", default="")
    )
    if not model:
        model = require_env("MODEL_ID")
    client = OpenAICompatibleClient(
        base_url=os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        or config_value(app_config, "llm", "main", "base_url"),
        api_key=os.getenv("OPENAI_COMPATIBLE_API_KEY")
        or config_value(app_config, "llm", "main", "api_key", default=""),
        thinking_enabled=config_value(
            app_config, "llm", "main", "enable_thinking"
        ),
    )
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
    interrupt=None,
    memory=None,
    app_config=None,
) -> ChannelHost | None:
    app_config = app_config or {}
    host = ChannelHost(
        lambda channel: ChannelContext(
            bus=bus,
            session_manager=session_manager,
            event_bus=event_bus,
            workspace=workdir,
            log=logging.getLogger("channels.%s" % channel.name),
            interrupt=interrupt,
            memory=memory,
        )
    )
    added = False
    chat_config = config_value(app_config, "channels", "chat", default={}) or {}
    if enable_web or _env_bool(
        "KIRAKIRA_WEB_ENABLED", bool(chat_config.get("enabled", False))
    ):
        host.add(
            WebChannel(
                host=os.getenv("KIRAKIRA_WEB_HOST", str(chat_config.get("host") or "127.0.0.1")),
                port=int(os.getenv("KIRAKIRA_WEB_PORT", str(chat_config.get("port") or 8765))),
                channel_name=os.getenv("KIRAKIRA_WEB_CHANNEL", str(chat_config.get("channel_name") or "web")),
            )
        )
        added = True
    telegram_config = config_value(app_config, "channels", "telegram", default={}) or {}
    telegram_token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or str(telegram_config.get("token") or "")
    ).strip()
    if enable_telegram or _env_bool(
        "KIRAKIRA_TELEGRAM_ENABLED",
        bool(telegram_config.get("enabled", bool(telegram_token))),
    ):
        if not telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required when Telegram channel is enabled")
        host.add(
            TelegramChannel(
                token=telegram_token,
                allow_from=_env_list("TELEGRAM_ALLOW_FROM")
                or [str(item) for item in telegram_config.get("allow_from", [])],
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
    if enable_qq or _env_bool(
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
                access_token=os.getenv("ONEBOT_ACCESS_TOKEN", ""),
                allow_from=_env_list("QQ_ALLOW_FROM")
                or [str(item) for item in qq_config.get("allow_from", [])],
                group_allow=_env_list("QQ_GROUP_ALLOW") or configured_group_ids,
                group_policies=group_policies,
                require_at=_env_bool(
                    "QQ_REQUIRE_AT", bool(qq_config.get("require_at", True))
                ),
                channel_name=os.getenv(
                    "KIRAKIRA_QQ_CHANNEL", str(qq_config.get("channel_name") or "qq")
                ),
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
    config_path: Path | None = None,
) -> CoreRuntime:
    load_dotenv(workdir / ".env")
    app_config = load_toml_config(config_path or workdir / "config.toml")
    model = os.getenv("MODEL_ID") or str(
        config_value(app_config, "llm", "main", "model", default="")
    )
    if not model:
        model = require_env("MODEL_ID")
    client = OpenAICompatibleClient(
        base_url=os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        or config_value(app_config, "llm", "main", "base_url"),
        api_key=os.getenv("OPENAI_COMPATIBLE_API_KEY")
        or config_value(app_config, "llm", "main", "api_key", default=""),
        thinking_enabled=config_value(
            app_config, "llm", "main", "enable_thinking"
        ),
    )
    bus = MessageBus()
    event_bus = EventBus()
    session_manager = SessionManager(workdir)
    memory = MemoryRuntime(workdir, session_manager=session_manager)
    embedding_model = os.getenv("EMBEDDING_MODEL_ID") or str(
        config_value(app_config, "memory", "embedding", "model", default="")
    )
    embedding_base_url = os.getenv("EMBEDDING_BASE_URL") or str(
        config_value(app_config, "memory", "embedding", "base_url", default="")
    )
    if embedding_model and embedding_base_url:
        memory.configure_embeddings(
            model=embedding_model,
            base_url=embedding_base_url,
            api_key=os.getenv("EMBEDDING_API_KEY")
            or str(
                config_value(
                    app_config, "memory", "embedding", "api_key", default=""
                )
            ),
        )
    registry = build_default_registry(workdir, memory=memory, session_manager=session_manager, bus=bus)
    # 能力快照：MCP 换代只切换 current 快照，在途 turn 用完旧租约后旧进程才断开。
    snapshot_store = RuntimeSnapshotStore()
    mcp_publisher = McpCatalogPublisher(snapshot_store)
    snapshot_store.set_drain_handler(mcp_publisher.drain_snapshot)
    # workspace MCP 由 mcp/servers/*.toml 声明并热重载；首轮 reconcile 失败不阻塞启动，
    # watcher 会在声明修好后自动重试。
    mcp_watcher = WorkspaceMcpWatcher(workdir / "mcp" / "servers", mcp_publisher)
    try:
        await mcp_watcher.reconcile()
    except (OSError, ValueError, RuntimeError) as error:
        mcp_watcher.last_error = str(error)
        logging.getLogger(__name__).error("workspace MCP initial publish failed: %s", error)
    context = ContextBuilder(
        workdir,
        memory,
        system_prompt=str(
            config_value(app_config, "agent", "system_prompt", default="")
        ),
    )
    # 未显式配置 memory_window / max_tokens 时，按模型真实 context_window 等比例派生。
    derived = recommended_context_settings(
        int(config_value(app_config, "llm", "main", "context_window", default=128_000)),
        float(
            config_value(
                app_config, "agent", "context", "effective_context_percent", default=0.9
            )
        ),
    )
    config = RuntimeConfig(
        model=model,
        max_iterations=int(
            os.getenv(
                "AGENT_MAX_ITERATIONS",
                str(config_value(app_config, "agent", "max_iterations", default=10)),
            )
        ),
        max_tokens=int(
            os.getenv(
                "AGENT_MAX_TOKENS",
                str(
                    config_value(
                        app_config, "agent", "max_tokens", default=derived.output_reserve
                    )
                ),
            )
        ),
        history_window=int(
            os.getenv(
                "AGENT_HISTORY_WINDOW",
                str(
                    config_value(
                        app_config,
                        "agent",
                        "context",
                        "memory_window",
                        default=derived.memory_window,
                    )
                ),
            )
        ),
        model_timeout_seconds=float(os.getenv("AGENT_MODEL_TIMEOUT", "120")),
        repeated_tool_call_limit=int(os.getenv("AGENT_REPEATED_TOOL_LIMIT", "3")),
        stream=_env_bool("AGENT_STREAM", True),
    )
    reasoner = DefaultReasoner(
        model_client=client,
        tools=registry,
        config=config,
        context=context,
        event_bus=event_bus,
    )
    subagents = SubagentManager(
        reasoner=reasoner,
        tools=registry,
        sessions=session_manager,
        memory=memory,
        bus=bus,
    )
    subagents.register_tool()
    scheduler = SchedulerService(
        workdir / ".kirakira" / "schedules.json",
        bus=bus,
        tools=registry,
    )
    pipeline = PassiveTurnPipeline(
        bus=bus,
        event_bus=event_bus,
        session_manager=session_manager,
        memory=memory,
        tools=registry,
        reasoner=reasoner,
        config=config,
        snapshot_store=snapshot_store,
    )
    loop = AgentLoop(bus=bus, pipeline=pipeline)
    plugin_manager = PluginManager(
        [workdir / ".kirakira" / "plugins", workdir / "plugins"],
        event_bus=event_bus,
        tool_registry=registry,
        workspace=workdir,
        session_manager=session_manager,
        memory=memory,
        mcp_publisher=mcp_publisher,
        skill_loader=context.skills,
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
        interrupt=loop.request_interrupt,
        memory=memory,
        app_config=app_config,
    )
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
                )
            )
        for plugin_channel in plugin_manager.channels:
            channel_host.add(plugin_channel)
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
        plugin_manager=plugin_manager,
        mcp_watcher=mcp_watcher,
        scheduler=scheduler,
        subagents=subagents,
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
    tasks = await runtime.start_background(start_channels=False)
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
        await runtime.stop_background(tasks)


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


def resolve_workspace(
    cli_workspace: str | None,
    app_config: JsonDict,
    *,
    default: Path,
) -> Path:
    """按 --workspace > KIRAKIRA_WORKSPACE > config [runtime].workspace > 默认解析。

    运行时可写状态（session、记忆、附件、插件数据、workspace MCP）全部落在这个根下，
    不同 workspace 之间互不共享。
    """

    env_value = os.getenv("KIRAKIRA_WORKSPACE")
    configured = str(config_value(app_config, "runtime", "workspace", default="") or "")
    for candidate in (cli_workspace, env_value, configured):
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue
        return Path(text).expanduser().resolve()
    return default


async def _main_async(args: argparse.Namespace, workdir: Path) -> None:
    runtime = await build_runtime(
        workdir,
        enable_web=args.web,
        enable_telegram=args.telegram,
        enable_qq=args.qq,
        config_path=args.config_path,
    )
    if args.serve or args.web or args.telegram or args.qq:
        await runtime_serve(runtime)
    else:
        await runtime_repl(runtime, workdir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kirakira Agent")
    parser.add_argument("--serve", action="store_true", help="Run background agent loop and configured channels.")
    parser.add_argument("--web", action="store_true", help="Enable stdlib web channel.")
    parser.add_argument("--telegram", action="store_true", help="Enable Telegram Bot API channel.")
    parser.add_argument("--qq", action="store_true", help="Enable QQ OneBot webhook channel.")
    parser.add_argument(
        "--workspace",
        default=None,
        help=(
            "Runtime state root (sessions, memory, plugin data, workspace MCP). "
            "Overrides KIRAKIRA_WORKSPACE and config [runtime].workspace."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.toml. Defaults to ./config.toml.",
    )
    args = parser.parse_args()
    # config 先于 workspace 解析：workspace 可以写在 config 里，但 config 本身不住在
    # workspace 内，否则会形成先有鸡还是先有蛋。
    cwd = Path(os.getcwd()).resolve()
    args.config_path = (
        Path(args.config).expanduser().resolve() if args.config else cwd / "config.toml"
    )
    workdir = resolve_workspace(
        args.workspace, load_toml_config(args.config_path), default=cwd
    )
    workdir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_main_async(args, workdir))
