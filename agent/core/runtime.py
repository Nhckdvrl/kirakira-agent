"""Own runtime startup, shutdown and direct conversation entry."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, List

from agent.composition import ResourceScope
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

    @property
    def working_directories(self):
        return self.tools.directory_provider

    _shutdown_scope: ResourceScope = field(
        default_factory=ResourceScope, init=False, repr=False
    )
    _shutdown_started: bool = field(default=False, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _background_tasks: list[asyncio.Task[Any]] = field(
        default_factory=list, init=False, repr=False
    )

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
        if self._shutdown_started:
            raise RuntimeError("runtime is shutting down")
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
        if self._started or self._shutdown_started:
            raise RuntimeError("runtime can only be started once")
        self._started = True
        tasks = self._background_tasks
        try:
            tasks.extend(
                [
                    asyncio.create_task(self.loop.run(), name="agent_loop"),
                    asyncio.create_task(
                        self.bus.dispatch_outbound(), name="bus_dispatch"
                    ),
                ]
            )
            if self.memory_services is not None:
                tasks.append(
                    asyncio.create_task(
                        self.memory_services.run_maintenance(),
                        name="memory_maintenance",
                    )
                )
            if start_channels and self.channel_host is not None:
                await self.channel_host.start_all()
            if self.scheduler is not None:
                tasks.append(
                    asyncio.create_task(self.scheduler.run(), name="scheduler")
                )
            if self.proactive_loop is not None:
                tasks.append(
                    asyncio.create_task(
                        self.proactive_loop.run(), name="proactive_loop"
                    )
                )
            if self.mcp_watcher is not None:
                tasks.append(
                    asyncio.create_task(
                        self.mcp_watcher.run(), name="workspace_mcp_watcher"
                    )
                )
            if self.plugin_watcher is not None:
                tasks.append(
                    asyncio.create_task(
                        self.plugin_watcher.run(), name="plugin_watcher"
                    )
                )
            if self.control_server is not None:
                # 控制面自己管连接 task,不进 tasks 列表;失败不能拖垮主链路。
                try:
                    await self.control_server.start()
                except Exception:
                    logger.exception(
                        "control plane failed to start; continuing without it"
                    )
                    self.control_server = None
            return tasks
        except BaseException as exc:
            try:
                await self.stop_background(tasks)
            except BaseException as cleanup_error:
                exc.add_note("startup rollback failed: " + repr(cleanup_error))
            raise

    async def stop_background(self, tasks: list[asyncio.Task[Any]]) -> None:
        """Close once, wait for physical disposal, and report all cleanup failures."""
        if not self._shutdown_started:
            self._shutdown_started = True
            owned = tuple(dict.fromkeys([*self._background_tasks, *tasks]))
            self._shutdown_scope.on_close(lambda: self._stop_background(owned))
        await self._shutdown_scope.close()

    async def _stop_background(self, tasks: tuple[asyncio.Task[Any], ...]) -> None:
        errors = []

        async def attempt(label, action):
            if action is None:
                return
            try:
                result = action()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                errors.append(RuntimeError(label + " cancelled during shutdown"))
            except Exception as exc:
                exc.add_note("runtime cleanup: " + label)
                errors.append(exc)

        # Stop new work first, then settle users of the resources being closed.
        self.loop.stop()
        for owner, method in (
            (self.control_server, "stop"),
            (self.scheduler, "stop"),
            (self.proactive_loop, "stop"),
            (self.mcp_watcher, "stop"),
            (self.plugin_watcher, "stop"),
            (self.control_service, "shutdown"),
            (self.control_runtime, "shutdown"),
            (self.subagents, "shutdown"),
            (self.scheduler, "shutdown"),
            (self.pipeline.admission, "close"),
            (self.loop, "shutdown"),
        ):
            await attempt(method, getattr(owner, method) if owner is not None else None)

        async def settle(selected):
            for task in selected:
                task.cancel()
            await asyncio.gather(*selected, return_exceptions=True)

        # Producers/maintenance must be gone before closing queues, tools or stores.
        await attempt(
            "background producers",
            lambda: settle([t for t in tasks if t.get_name() != "bus_dispatch"]),
        )

        async def drain():
            if not await self.bus.drain(timeout=10.0):
                logger.warning("outbound queue did not drain before shutdown")

        await attempt("outbound drain", drain)
        for owner, method in (
            (self.bus, "stop"),
            (self.bus, "shutdown"),
            (self.channel_host, "stop_all"),
            (self.tools, "shutdown"),
        ):
            await attempt(method, getattr(owner, method) if owner is not None else None)
        await attempt("background tasks", lambda: settle(tasks))
        for owner, method in (
            (self.plugin_manager, "terminate_all"),
            (self.mcp_watcher, "shutdown"),
            (self.drift_runner, "close"),
            (self.proactive_loop, "close"),
            (self.memory, "shutdown"),
            (self.memory_services, "aclose"),
            (self.event_bus, "shutdown"),
            (self.control_store, "close"),
            (self.session_manager, "close"),
            (self.reasoner.execution_store, "close"),
        ):
            await attempt(method, getattr(owner, method) if owner is not None else None)
        if errors:
            raise ExceptionGroup("runtime cleanup failed", errors)
