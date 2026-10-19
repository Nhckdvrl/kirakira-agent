"""Application composition root. Channel and proactive wiring have independent owners."""

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from agent.composition import ServiceRegistry
from agent.config import config_value, load_dotenv, load_toml_config, require_env
from agent.core.contracts import RuntimeConfig
from agent.core.execution import ExecutionStore
from agent.core.loop import AgentLoop
from agent.core.pipeline import PassiveTurnPipeline
from agent.core.reasoner import DefaultReasoner
from agent.core.runtime import CoreRuntime
from agent.mcp import McpCatalogPublisher, WorkspaceMcpAdmin, WorkspaceMcpWatcher
from agent.model_runtime.context_policy import recommended_context_settings
from agent.model_runtime.retry import ModelRetryPolicy
from agent.plugins import PluginManager
from agent.plugins.snapshot import RuntimeSnapshotStore
from agent.plugins.watcher import PluginWatcher
from agent.prompting.context_builder import ContextBuilder
from agent.restart import RestartCoordinator, SupervisorCommitChannel
from agent.subagent import SubagentManager
from bootstrap.channels import build_channel_host
from bootstrap.control import build_control_plane
from bootstrap.dashboard_api import DashboardService
from bootstrap.environment import env_bool
from bootstrap.memory import build_memory_services
from bootstrap.proactive import build_proactive
from bootstrap.restart import register_agent_restart_tool
from bootstrap.tools import build_tool_registry
from bus.event_bus import EventBus
from bus.queue import MessageBus
from core.memory.legacy import MemoryRuntime
from infra.channels.contract import ChannelContext
from infra.channels.host import ChannelHost
from infra.providers.model_client_adapter import ModelClientProvider
from infra.providers.step_plan import create_model_client
from plugins.default_memory.contract import MEMORY_READER, MEMORY_SERVICES, MemoryReader
from plugins.default_memory.inspector import RecallInspector
from plugins.scheduler.service import SchedulerService
from session.manager import SessionManager


async def build_runtime(
    workdir: Path,
    *,
    enable_web: bool = False,
    enable_telegram: bool = False,
    enable_qq: bool = False,
    enable_qqbot: bool = False,
    config_path: Path | None = None,
) -> CoreRuntime:
    async with AsyncExitStack() as rollback:
        runtime = await _compose_runtime(
            workdir,
            enable_web=enable_web,
            enable_telegram=enable_telegram,
            enable_qq=enable_qq,
            enable_qqbot=enable_qqbot,
            config_path=config_path,
            rollback=rollback,
        )
        rollback.pop_all()  # ownership transfers to CoreRuntime only after assembly succeeds
        return runtime


async def _compose_runtime(
    workdir: Path,
    *,
    enable_web: bool = False,
    enable_telegram: bool = False,
    enable_qq: bool = False,
    enable_qqbot: bool = False,
    config_path: Path | None = None,
    rollback: AsyncExitStack,
) -> CoreRuntime:
    load_dotenv(workdir / ".env")
    app_config = load_toml_config(config_path or workdir / "config.toml")
    # 主动目标必须有真实 Channel subscriber。配置 proactive target 后自动装配对应
    # 内置渠道，避免用户还要额外猜测 --telegram/--qq/--web 才能完成发送链。
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
    model = os.getenv("MODEL_ID") or str(
        config_value(app_config, "llm", "main", "model", default="")
    )
    if not model:
        model = require_env("MODEL_ID")
    client = create_model_client(
        reasoning_effort=config_value(
            app_config, "llm", "main", "reasoning_effort", default="medium"
        ),
        base_url=os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        or config_value(app_config, "llm", "main", "base_url"),
        api_key=os.getenv("OPENAI_COMPATIBLE_API_KEY")
        or config_value(app_config, "llm", "main", "api_key", default=""),
        thinking_enabled=config_value(app_config, "llm", "main", "enable_thinking"),
        context_window=int(
            config_value(app_config, "llm", "main", "context_window", default=0)
        ),
        effective_context_percent=float(
            config_value(
                app_config,
                "agent",
                "context",
                "effective_context_percent",
                default=0.9,
            )
        ),
    )
    memory_client: Any = client
    light_model = os.getenv("LIGHT_MODEL_ID") or str(
        config_value(app_config, "llm", "light", "model", default="") or ""
    )
    light_base_url = os.getenv("LIGHT_OPENAI_COMPATIBLE_BASE_URL") or str(
        config_value(app_config, "llm", "light", "base_url", default="") or ""
    )
    if light_model and light_base_url:
        from agent.model_runtime.fallback import ResilientModelClient

        light_client = create_model_client(
            reasoning_effort=config_value(
                app_config, "llm", "light", "reasoning_effort", default="medium"
            ),
            base_url=light_base_url,
            api_key=os.getenv("LIGHT_OPENAI_COMPATIBLE_API_KEY")
            or config_value(app_config, "llm", "light", "api_key", default="")
            or os.getenv("OPENAI_COMPATIBLE_API_KEY")
            or config_value(app_config, "llm", "main", "api_key", default=""),
            thinking_enabled=config_value(
                app_config, "llm", "light", "enable_thinking", default=False
            ),
            context_window=int(
                config_value(
                    app_config,
                    "llm",
                    "light",
                    "context_window",
                    default=config_value(
                        app_config, "llm", "main", "context_window", default=0
                    ),
                )
            ),
            effective_context_percent=float(
                config_value(
                    app_config,
                    "agent",
                    "context",
                    "effective_context_percent",
                    default=0.9,
                )
            ),
        )
        memory_client = ResilientModelClient(
            primary=light_client,
            primary_runtime_id="light",
            primary_model=light_model,
            fallback=client,
            fallback_model=model,
        )
    bus = MessageBus()
    from agent.tools.message_push import MessagePushTool

    push_tool = MessagePushTool(chat_lane=bus.chat_lane)
    event_bus = EventBus()
    session_manager = SessionManager(workdir)
    rollback.callback(session_manager.close)
    session_manager.recover_admissions()
    # 记忆 DI 缝先建立:引擎是 coremem.db 的唯一 owner,旧 MemoryRuntime 共享它的连接,
    # 也让显式记忆工具(Stage 5)能在注册表构建时就拿到引擎。
    memory_provider = ModelClientProvider(memory_client)
    memory_services = build_memory_services(
        app_config=app_config,
        workspace=workdir,
        provider=memory_provider,
        light_provider=memory_provider,
        event_publisher=event_bus,
        session_manager=session_manager,
        memory_window=int(
            config_value(app_config, "agent", "context", "memory_window", default=40)
        ),
    )
    rollback.push_async_callback(memory_services.aclose)
    capabilities = ServiceRegistry()
    capabilities.publish(
        "bootstrap:memory",
        {
            MEMORY_SERVICES: memory_services,
            MEMORY_READER: MemoryReader(memory_services.engine),
        },
    )
    memory = MemoryRuntime(
        workdir,
        session_manager=session_manager,
        engine=str(config_value(app_config, "memory", "engine", default="auto")),
        shared_store=memory_services.store,
        event_bus=event_bus,
    )
    rollback.push_async_callback(memory.shutdown)
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
                config_value(app_config, "memory", "embedding", "api_key", default="")
            ),
        )
    registry = build_tool_registry(
        workdir,
        memory=memory,
        session_manager=session_manager,
        bus=bus,
        push_tool=push_tool,
        memory_services=memory_services,
    )
    rollback.push_async_callback(registry.shutdown)
    # 能力快照：MCP 换代只切换 current 快照，在途 turn 用完旧租约后旧进程才断开。
    snapshot_store = RuntimeSnapshotStore()
    mcp_publisher = McpCatalogPublisher(snapshot_store)
    snapshot_store.set_drain_handler(mcp_publisher.drain_snapshot)
    # workspace MCP 由 mcp/servers/*.toml 声明并热重载；首轮 reconcile 失败不阻塞启动，
    # watcher 会在声明修好后自动重试。
    mcp_watcher = WorkspaceMcpWatcher(workdir / "mcp" / "servers", mcp_publisher)
    rollback.push_async_callback(mcp_watcher.shutdown)
    # 让 agent 也能自己增删声明，走的仍是 watcher 那条 reconcile 路径。
    WorkspaceMcpAdmin(workdir, mcp_watcher).register_tools(registry)
    try:
        await mcp_watcher.reconcile()
    except (OSError, ValueError, RuntimeError) as error:
        mcp_watcher.last_error = str(error)
        logging.getLogger(__name__).error(
            "workspace MCP initial publish failed: %s", error
        )
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
        context_window=int(
            config_value(app_config, "llm", "main", "context_window", default=0)
        ),
        effective_context_percent=float(
            config_value(
                app_config,
                "agent",
                "context",
                "effective_context_percent",
                default=0.9,
            )
        ),
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
                        app_config,
                        "agent",
                        "max_tokens",
                        default=derived.output_reserve,
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
        max_parallel_tool_calls=max(1, int(os.getenv("AGENT_MAX_PARALLEL_TOOLS", "4"))),
        stream=env_bool("AGENT_STREAM", True),
        retry_policy=ModelRetryPolicy(
            max_retries=int(
                config_value(app_config, "llm", "main", "max_retries", default=10)
            )
        ),
    )
    execution_store = ExecutionStore(workdir / ".kirakira" / "executions.db")
    rollback.callback(execution_store.close)
    execution_store.recover()
    reasoner = DefaultReasoner(
        model_client=client,
        tools=registry,
        config=config,
        context=context,
        event_bus=event_bus,
        execution_store=execution_store,
    )
    subagents = SubagentManager(
        reasoner=reasoner,
        tools=registry,
        sessions=session_manager,
        memory=memory,
        bus=bus,
    )
    rollback.push_async_callback(subagents.shutdown)
    subagents.register_tool()
    # 检索回放:记录每轮召回了什么、注入了没有;引擎未承重时这条路本来就不走。
    recall_inspector = RecallInspector(workdir)
    reasoner.add_tool_hooks([recall_inspector.tool_hook()])
    pipeline = PassiveTurnPipeline(
        bus=bus,
        event_bus=event_bus,
        session_manager=session_manager,
        memory=memory,
        tools=registry,
        reasoner=reasoner,
        config=config,
        snapshot_store=snapshot_store,
        memory_services=memory_services,
        recall_inspector=recall_inspector,
        service_registry=capabilities,
    )
    scheduler = SchedulerService(
        workdir / ".kirakira" / "schedules.json",
        bus=bus,
        tools=registry,
        pipeline=pipeline,
        sessions=session_manager,
        executions=execution_store,
    )
    rollback.push_async_callback(scheduler.shutdown)
    loop = AgentLoop(bus=bus, pipeline=pipeline)
    bundled_plugin_root = (
        Path(__file__).resolve().parents[1] / "plugin_packages" / "curated_feeds"
    )
    plugin_roots = [workdir / ".kirakira" / "plugins"]
    if bundled_plugin_root.is_dir():
        plugin_roots.append(bundled_plugin_root)
    plugin_manager = PluginManager(
        plugin_roots,
        event_bus=event_bus,
        tool_registry=registry,
        workspace=workdir,
        mcp_publisher=mcp_publisher,
        skill_loader=context.skills,
        service_registry=capabilities,
    )
    rollback.push_async_callback(plugin_manager.terminate_all)
    await plugin_manager.load_all()
    # 在途 turn 持有各插件当前代际租约;热重载换代不会抽走 turn 正在用的能力。
    pipeline.plugin_generations = plugin_manager.generations
    plugin_watcher = PluginWatcher(plugin_manager)
    # 安装/卸载/启停后立即热重载,不必重启进程。
    plugin_manager.reload_hook = plugin_watcher.wake
    reasoner.add_tool_hooks(plugin_manager.tool_hooks)
    reasoner.add_prompt_render_plugin_modules(plugin_manager.prompt_render_modules)
    reasoner.add_before_step_plugin_modules(plugin_manager.before_step_modules)
    reasoner.add_after_step_plugin_modules(plugin_manager.after_step_modules)
    pipeline.add_before_turn_plugin_modules(plugin_manager.before_turn_modules)
    pipeline.add_before_reasoning_plugin_modules(
        plugin_manager.before_reasoning_modules
    )
    pipeline.add_after_reasoning_plugin_modules(plugin_manager.after_reasoning_modules)
    pipeline.add_after_turn_plugin_modules(plugin_manager.after_turn_modules)
    # Dashboard 数据面。主动/Drift 在下面才装配,这里先建再回填——DashboardService
    # 是可变 dataclass,这样不必为了一个只读面板重排整条装配顺序。
    dashboard = DashboardService(
        workspace=workdir,
        session_manager=session_manager,
        memory_services=memory_services,
        memory=memory,
        plugin_manager=plugin_manager,
        restart_coordinator=None,
        recall_inspector=recall_inspector,
        # 状态库连接归本循环所在线程独占;Web 的 HTTP handler 在别的线程,读要 marshal 回来。
        loop=asyncio.get_running_loop(),
    )
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
        rollback.push_async_callback(channel_host.stop_all)
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
    proactive_loop, drift_runner = build_proactive(
        workdir=workdir,
        app_config=app_config,
        default_model=model,
        bus=bus,
        session_manager=session_manager,
        memory=memory,
        client=client,
        passive_busy_fn=loop.is_busy,
        memory_services=memory_services,
        plugin_sources=plugin_manager.proactive_sources,
        tool_registry=registry,
        # tick 与被动 turn 同一对租约保证:换代不抽走在途能力。
        plugin_generations=plugin_manager.generations,
        snapshot_store=snapshot_store,
        drift_skill_roots_provider=lambda: plugin_manager.drift_skill_roots,
        execution_store=execution_store,
    )
    if proactive_loop is not None:
        rollback.callback(proactive_loop.close)
    if drift_runner is not None:
        rollback.callback(drift_runner.close)
    # 回填晚于面板构造的两条链路,Dashboard 的主动/Drift 面板由此拿到真实数据。
    dashboard.proactive_loop = proactive_loop
    dashboard.drift_runner = drift_runner
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

    async def _control_consolidate(thread_id: str) -> bool:
        """thread/consolidate/start 的执行体:强制归档一个 thread 并报告是否有变化。"""
        maintenance = getattr(memory_services, "markdown", None)
        maintenance = getattr(maintenance, "maintenance", None)
        if maintenance is None:
            raise RuntimeError("当前 runtime 没有配置记忆归档能力")
        from core.memory.markdown import ConsolidateRequest

        session = session_manager.get_or_create(thread_id)
        before = int(session.last_consolidated or 0)
        await maintenance.consolidate(
            ConsolidateRequest(
                session=session,
                force=True,
                scope_channel=str(session.metadata.get("channel") or ""),
                scope_chat_id=str(session.metadata.get("chat_id") or ""),
            )
        )
        changed = int(session.last_consolidated or 0) > before
        if changed:
            await session_manager.save_async(session)
        return changed

    # supervisor 托管时装配重启协调器:supervisor(Reference 原文)早已在等私有管道上
    # 唯一一帧 restart_commit + 退出码 75,这里补进程内的另一半。非托管运行为 None,
    # agent_restart 不注册,准入永远开放。声明 supervised 却缺 fd 是环境契约被破坏,
    # from_environment 直接抛错(fail loud),不静默降级。
    restart_coordinator = None
    commit_channel = SupervisorCommitChannel.from_environment()
    if commit_channel is not None:
        restart_coordinator = RestartCoordinator(
            commit_channel.boot_id,
            supervised=True,
            commit=commit_channel.commit,
        )

    # 控制面:workspace 私有 Unix socket 上的 JSON-RPC,让外部程序观测/驱动 agent。
    control_store, control_runtime, control_service, control_server = (
        build_control_plane(
            workspace=workdir,
            pipeline=pipeline,
            sessions=session_manager,
            endpoint=os.getenv("KIRAKIRA_CONTROL_ENDPOINT", "").strip() or None,
            workspace_token=os.getenv("KIRAKIRA_CONTROL_TOKEN", "").strip() or None,
            boot_id=commit_channel.boot_id if commit_channel is not None else None,
            plugin_drain=plugin_manager.reconcile_disabled_and_drain,
            consolidate=_control_consolidate,
            restart_coordinator=restart_coordinator,
        )
    )
    rollback.callback(control_store.close)
    rollback.push_async_callback(control_runtime.shutdown)
    rollback.push_async_callback(control_service.shutdown)
    rollback.push_async_callback(control_server.stop)
    dashboard.restart_coordinator = restart_coordinator
    if restart_coordinator is not None:
        register_agent_restart_tool(registry, restart_coordinator)
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
        plugin_watcher=plugin_watcher,
        memory_services=memory_services,
        service_registry=capabilities,
        scheduler=scheduler,
        subagents=subagents,
        proactive_loop=proactive_loop,
        drift_runner=drift_runner,
        control_store=control_store,
        control_runtime=control_runtime,
        control_service=control_service,
        control_server=control_server,
        restart_coordinator=restart_coordinator,
    )
