"""Own runtime startup, shutdown and direct conversation entry."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List

from agent.composition import ResourceScope
from bus.event_bus import EventBus
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus
from core.memory.legacy import MemoryRuntime
from core.schema import JsonDict
from infra.channels.host import ChannelHost
from plugins.context.context_builder import ContextBuilder
from plugins.conversation.service import ConversationService
from plugins.ledger.manager import SessionManager
from plugins.ledger.log import MessageLog
from plugins.turn_projection.contract import TurnProjection
from plugins.react.reasoner import ReactProgram
from plugins.reply.follower import ReplyFollower
from plugins.tools.hooks import ToolHook
from plugins.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class Application:
    bus: MessageBus
    event_bus: EventBus
    session_manager: SessionManager
    memory: MemoryRuntime
    tools: ToolRegistry
    context: ContextBuilder
    reasoner: ReactProgram
    pipeline: ConversationService
    loop: ReplyFollower
    message_log: MessageLog | None = None
    turn_projection: TurnProjection | None = None
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
    composition: Any | None = None
    _service_fields: dict = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_composition(cls, root):
        from bootstrap.distribution import application_fields

        values = application_fields(root)
        bindings = values.pop("_service_fields")
        application = cls(**values, service_registry=root.services, composition=root)
        application._service_fields = bindings
        return application

    def __getattribute__(self, name):
        # The public facade follows the graph; it never revives a retired owner.
        state = object.__getattribute__(self, "__dict__")
        binding = state.get("_service_fields", {}).get(name)
        root = state.get("composition")
        if binding is not None and root is not None and not root._closed:
            key, index = binding
            try:
                value = root.services.resolve(key)
            except LookupError:
                if not state.get("_shutdown_started"):
                    raise
            else:
                return value if index is None else value[index]
        return object.__getattribute__(self, name)

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
        request_id: str | None = None,
        resume_program_id: str | None = None,
        supersede: bool = False,
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
        if request_id is not None:
            metadata["client_request_id"] = request_id
        if resume_program_id is not None:
            metadata["resume_program_id"] = resume_program_id
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
            supersede=supersede,
        )

    async def start_background(
        self, *, start_channels: bool = True
    ) -> list[asyncio.Task[Any]]:
        if self._started or self._shutdown_started:
            raise RuntimeError("runtime can only be started once")
        self._started = True
        tasks = self._background_tasks
        try:
            await self.composition.start_runtime(start_channels=start_channels)
            tasks.extend(self.composition.tasks())
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
        external = [task for task in tasks if task not in self.composition.tasks()]
        for task in external:
            task.cancel()
        await asyncio.gather(*external, return_exceptions=True)
        for name in self._service_fields:
            try:
                object.__setattr__(self, name, getattr(self, name))
            except LookupError:
                pass
        await self.composition.close()
