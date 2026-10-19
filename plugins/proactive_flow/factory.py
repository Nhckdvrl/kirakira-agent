"""Compose Wake sources and the Drift runtime."""

import logging
from pathlib import Path
from typing import Any

from bus.queue import MessageBus
from core.memory.legacy import MemoryRuntime
from core.schema import JsonDict
from plugins.drift_flow import DriftRunner
from plugins.ledger.manager import SessionManager
from plugins.models.types import ModelClient
from plugins.wake_proactive.mcp_sources import (
    ToolRegistryMcpGateway,
    compile_proactive_sources,
)
from plugins.wake_proactive.sources import build_file_inbox_registry
from plugins.wake_proactive.state import ProactiveStateStore
from proactive_v2 import ProactiveLoop
from proactive_v2.config import ProactiveConfig


def build_source_registry(
    workdir: Path,
    plugin_sources: Any = None,
    tool_registry: Any = None,
    gateway: Any = None,
):
    """内置文件源 + 插件声明编译出的 MCP 源。

    插件只声明 ProactiveSourceSpec,由这里编译成真实 ProactiveSource——
    这是主动链路可扩展的接线点。单个源注册失败不阻断其余源与整条链路。
    """
    registry = build_file_inbox_registry(workdir)
    for source in compile_proactive_sources(
        list(plugin_sources or []), tool_registry, gateway=gateway
    ):
        try:
            registry.add(source)
        except ValueError as error:
            logging.getLogger(__name__).warning(
                "plugin proactive source skipped: %s", error
            )
    return registry


def build_proactive(
    *,
    workdir: Path,
    app_config: JsonDict,
    default_model: str,
    bus: MessageBus,
    session_manager: SessionManager,
    memory: MemoryRuntime,
    client: ModelClient,
    passive_busy_fn: Any | None = None,
    memory_services: Any = None,
    plugin_sources: Any = None,
    tool_registry: Any = None,
    drift_skill_roots_provider: Any = None,
    execution_store: Any = None,
) -> tuple[ProactiveLoop | None, DriftRunner | None]:
    """按配置装配主动推送链路与 Drift 链路；未启用则返回 (None, None)。"""
    cfg = ProactiveConfig.from_app_config(app_config, default_model=default_model)
    if not cfg.enabled:
        return None, None
    if not cfg.target_ready:
        logging.getLogger(__name__).warning(
            "[proactive] enabled=true 但未配置 target.channel/chat_id，主动链路不启动"
        )
        return None, None

    # 长期档案读取绑定 coremem 的 markdown store。Reference 里 Drift/主动读的
    # 就是 MEMORY.md(MemoryProfileApi),不是引擎;这里同口径,不再经过旧 MemoryRuntime。
    markdown_runtime = getattr(memory_services, "markdown", None)
    long_term_source = getattr(markdown_runtime, "store", None) or memory

    drift_runner: DriftRunner | None = None
    drift_hook = None
    if cfg.drift.enabled:
        drift_runner = DriftRunner(
            config=cfg.drift,
            workspace=workdir,
            bus=bus,
            session_manager=session_manager,
            model_client=client,
            model=cfg.model,
            memory=long_term_source,
            memory_services=memory_services,
            target_channel=cfg.channel,
            target_chat_id=cfg.chat_id,
            skill_roots_provider=drift_skill_roots_provider,
            passive_busy_fn=passive_busy_fn,
            execution_store=execution_store,
        )
        drift_hook = drift_runner.maybe_run

    # 所有插件源共用一个 gateway;tick 开始时 loop 把租到的快照钉在上面,
    # 本轮 fetch/ack 用同一代 MCP 工具视图。
    mcp_gateway = ToolRegistryMcpGateway(tool_registry)
    loop = ProactiveLoop(
        config=cfg,
        bus=bus,
        session_manager=session_manager,
        model_client=client,
        sources=build_source_registry(
            workdir, plugin_sources, tool_registry, gateway=mcp_gateway
        ),
        state=ProactiveStateStore(workdir / "proactive.db"),
        memory=long_term_source,
        drift_hook=drift_hook,
        passive_busy_fn=passive_busy_fn,
        execution_store=execution_store,
        memory_services=memory_services,
        mcp_gateway=mcp_gateway,
    )
    return loop, drift_runner
