"""Own runtime startup, shutdown and direct conversation entry."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, List

from agent.core.loop import AgentLoop
from agent.core.pipeline import PassiveTurnPipeline
from agent.core.reasoner import DefaultReasoner
from agent.prompting.context_builder import ContextBuilder
from agent.tool_hooks import ToolHook
from agent.tools.registry import ToolRegistry
from bus.event_bus import EventBus
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus
from core.memory.legacy import MemoryRuntime
from core.schema import JsonDict
from infra.channels.host import ChannelHost
from session.manager import SessionManager

logger = logging.getLogger(__name__)


@dataclass
class CoreRuntime:
    bus: MessageBus
    event_bus: EventBus
    session_manager: SessionManager
    memory: MemoryRuntime
    tools: ToolRegistry
    context: ContextBuilder
    reasoner: DefaultReasoner
    pipeline: PassiveTurnPipeline
    loop: AgentLoop
    channel_host: ChannelHost | None = None
    plugin_manager: Any | None = None
    mcp_watcher: Any | None = None
    plugin_watcher: Any | None = None
    service_registry: Any = None
    memory_services: Any = None
    scheduler: Any | None = None
    subagents: Any | None = None
    proactive_loop: Any | None = None
    drift_runner: Any | None = None
    control_store: Any | None = None
    control_runtime: Any | None = None
    control_service: Any | None = None
    control_server: Any | None = None
    # supervisor 托管时非空;runtime_serve 等待它的 committed 事件并以 75 退出换代。
    restart_coordinator: Any | None = None

    def add_tool_hooks(self, hooks: List[ToolHook]) -> None:
        self.reasoner.add_tool_hooks(hooks)

    async def process_direct(
        self,
        content: str,
        *,
        session_key: str = "direct:local",
        channel: str = "direct",
        chat_id: str = "local",
        omit_user_turn: bool = False,
        skip_post_memory: bool = False,
        skip_memory_retrieval: bool = False,
        disabled_tools: List[str] | None = None,
    ) -> OutboundMessage:
        metadata: JsonDict = {
            "session_key_override": session_key,
            "omit_user_turn": omit_user_turn,
            "skip_post_memory": skip_post_memory,
            "skip_memory_retrieval": skip_memory_retrieval,
        }
        if disabled_tools:
            metadata["disabled_tools"] = list(disabled_tools)
        return await self.pipeline.run(
            InboundMessage(
                channel=channel,
                sender="direct_user",
                chat_id=chat_id,
                content=content,
                metadata=metadata,
            ),
            session_key,
            dispatch_outbound=False,
        )

    async def start_background(
        self, *, start_channels: bool = True
    ) -> list[asyncio.Task[Any]]:
        tasks = [
            asyncio.create_task(self.loop.run(), name="agent_loop"),
            asyncio.create_task(self.bus.dispatch_outbound(), name="bus_dispatch"),
        ]
        if self.memory_services is not None:
            tasks.append(
                asyncio.create_task(
                    self.memory_services.run_maintenance(), name="memory_maintenance"
                )
            )
        if start_channels and self.channel_host is not None:
            await self.channel_host.start_all()
        if self.scheduler is not None:
            tasks.append(asyncio.create_task(self.scheduler.run(), name="scheduler"))
        if self.proactive_loop is not None:
            tasks.append(
                asyncio.create_task(self.proactive_loop.run(), name="proactive_loop")
            )
        if self.mcp_watcher is not None:
            tasks.append(
                asyncio.create_task(
                    self.mcp_watcher.run(), name="workspace_mcp_watcher"
                )
            )
        if self.plugin_watcher is not None:
            tasks.append(
                asyncio.create_task(self.plugin_watcher.run(), name="plugin_watcher")
            )
        if self.control_server is not None:
            # 控制面自己管连接 task,不进 tasks 列表;失败不能拖垮主链路。
            try:
                await self.control_server.start()
            except Exception:
                logger.exception("control plane failed to start; continuing without it")
                self.control_server = None
        return tasks

    async def stop_background(self, tasks: list[asyncio.Task[Any]]) -> None:
        # 先停控制面:不再接受新的 programmatic turn,在途 turn 由
        # ConversationRuntime.shutdown 取消并写入终态。
        if self.control_server is not None:
            await self.control_server.stop()
        if self.control_service is not None:
            await self.control_service.shutdown()
        if self.control_runtime is not None:
            await self.control_runtime.shutdown()
        if self.subagents is not None:
            await self.subagents.shutdown()
        await self.loop.shutdown()
        if self.scheduler is not None:
            self.scheduler.stop()
        if self.proactive_loop is not None:
            self.proactive_loop.stop()
        if self.mcp_watcher is not None:
            self.mcp_watcher.stop()
        if self.plugin_watcher is not None:
            self.plugin_watcher.stop()
        drained = await self.bus.drain(timeout=10.0)
        if not drained:
            logger.warning("outbound queue did not drain before shutdown")
        self.bus.stop()
        await self.bus.shutdown()
        if self.channel_host is not None:
            await self.channel_host.stop_all()
        await self.tools.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.plugin_manager is not None:
            await self.plugin_manager.terminate_all()
        if self.mcp_watcher is not None:
            await self.mcp_watcher.shutdown()
        if self.drift_runner is not None:
            self.drift_runner.close()
        if self.proactive_loop is not None:
            self.proactive_loop.close()
        await self.memory.shutdown()
        # 引擎持有 coremem.db 与 embedder,必须在旧 MemoryRuntime 之后关闭。
        if self.memory_services is not None:
            try:
                await self.memory_services.aclose()
            except Exception:
                logger.exception("memory services shutdown failed")
        await self.event_bus.shutdown()
        if self.control_store is not None:
            self.control_store.close()
        self.session_manager.close()
        if self.reasoner.execution_store is not None:
            self.reasoner.execution_store.close()
