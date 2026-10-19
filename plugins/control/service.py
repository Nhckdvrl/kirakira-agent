from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from plugins.control.errors import ThreadBusyError, ThreadNotFoundError
from plugins.control.ids import new_thread_id
from plugins.control.models import ThreadRecord, ThreadSource, TurnRequest
from plugins.control.protocol.errors import UNAUTHORIZED, JsonRpcError
from plugins.control.protocol.models import InitializeParams
from plugins.control.runtime import ConversationRuntime, TurnHandle
from plugins.control.store import ControlStore
from plugins.ledger.manager import SessionManager


class ControlService:
    """把协议方法投影到唯一 ConversationRuntime 和 SessionManager。"""

    def __init__(
        self,
        runtime: ConversationRuntime,
        sessions: SessionManager,
        store: ControlStore,
        workspace: Path,
        *,
        plugin_drain: Callable[[str], Awaitable[str]] | None = None,
        consolidate: Callable[[str], Awaitable[bool]] | None = None,
        workspace_token: str | None = None,
        boot_id: str | None = None,
        ready: Callable[[], bool] | None = None,
        restart_coordinator: Any = None,
        workspace_services: Any = None,
        programs: Any = None,
    ) -> None:
        self.runtime = runtime
        self.sessions = sessions
        self.store = store
        self.workspace = workspace.resolve()
        self.workspace_services = workspace_services
        self.programs = programs
        self._plugin_drain = plugin_drain
        self._consolidate = consolidate
        self._workspace_token = workspace_token
        self._boot_id = boot_id
        self._ready = ready
        self._restart_coordinator = restart_coordinator
        self._operation_tasks: set[asyncio.Task[dict[str, object]]] = set()

    def initialize(self, params: InitializeParams) -> dict[str, object]:
        if self._workspace_token is not None and not secrets.compare_digest(
            params.workspaceToken or "",
            self._workspace_token,
        ):
            raise JsonRpcError(UNAUTHORIZED, "Invalid workspace token")
        return {
            "protocolVersion": "1.0",
            "serverInfo": {"name": "kirakira-agent", "version": "0.1.0"},
            "workspace": str(self.workspace),
            "capabilities": {
                "reasoningEvents": False,
                "turnInterrupt": True,
                "turnSteer": False,
            },
        }

    def status(self) -> dict[str, object]:
        return {
            "ready": self._ready() if self._ready is not None else True,
            "bootId": self._boot_id,
            "workspace": str(self.workspace),
            "protocolVersion": "1.0",
        }

    async def workspace_request(self, method, params):
        from plugins.control.protocol.models import METHOD_PARAMS
        from utils.async_tasks import run_sync_drained

        if method == "thread/start":
            values = METHOD_PARAMS[method].validate(params)
            return await run_sync_drained(self.start_thread, values["metadata"])
        if (
            isinstance(method, str)
            and method.startswith("program/")
            and method in METHOD_PARAMS
        ):
            values = METHOD_PARAMS[method].validate(params)
            if method == "program/resume":
                handle = await self.resume_program(
                    values["threadId"], values["programId"]
                )
                return handle.record()
            return await run_sync_drained(self.program_call, method, values)
        if (
            not isinstance(method, str)
            or not method.startswith(("project/", "directory/"))
            or method not in METHOD_PARAMS
        ):
            raise ValueError("unsupported workspace method")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        values = METHOD_PARAMS[method].validate(params)
        if method == "directory/set":
            return await self.set_directory(values)
        return await run_sync_drained(self.workspace_call, method, values)

    def program_call(self, method, values):
        self._thread_record(values["threadId"])
        if self.programs is None:
            raise ValueError("reply programs are unavailable")
        if method == "program/list":
            return {
                "items": self.programs.store.list(values["threadId"], values["limit"])
            }
        if method == "program/read":
            return self.programs.store.read(values["programId"], values["threadId"])
        if method == "program/resolve":
            return self.programs.store.resolve(
                values["programId"],
                values["threadId"],
                values["effectKey"],
                values["status"],
                values["content"],
                values["evidence"],
                values["expectedRevision"],
            )
        raise ValueError("unknown program method")

    async def resume_program(self, thread_id, program_id):
        self._thread_record(thread_id)
        if self.programs is None:
            raise ValueError("reply programs are unavailable")
        message = self.programs.resume_request(program_id, thread_id)
        return await self.runtime.start_turn(
            TurnRequest(thread_id, message.content, {"resume_program_id": program_id})
        )

    def workspace_call(self, method, values):
        services = self.workspace_services
        if services is None:
            raise ValueError("workspace capabilities are unavailable")
        projects, directories = services.projects, services.directories
        if method == "project/list":
            return {"items": projects.list()}
        if method == "project/create":
            return projects.create(
                values["projectId"], values["name"], values["directory"]
            )
        if method == "project/update":
            return projects.update(
                values["projectId"],
                name=values["name"],
                archived=values["archived"],
                expected_version=values["expectedRevision"],
            )
        if method == "project/bindDirectory":
            return projects.bind_directory(values["projectId"], values["path"])
        if method == "directory/browse":
            return directories.browse(values["path"], values["after"])
        if method == "directory/current":
            self._thread_record(values["threadId"])
            return directories.current_info(values["threadId"])
        raise ValueError("unknown workspace method")

    async def set_directory(self, values):
        self._thread_record(values["threadId"])
        services = self.workspace_services
        if services is None or services.admission is None:
            raise ValueError("directory capability is unavailable")
        from utils.async_tasks import run_sync_drained

        async with services.admission.hold(values["threadId"]):
            return await run_sync_drained(
                services.directories.switch,
                values["threadId"],
                values["path"],
                receipt_key="control:" + values["threadId"] + ":" + values["requestId"],
                expected_version=values["expectedRevision"],
            )

    def notify_turn_delivered(self, turn_id: str) -> None:
        """terminal 事件已送达客户端 → 重启提交的第二个条件成立。"""
        if self._restart_coordinator is not None:
            self._restart_coordinator.mark_delivered(turn_id)

    def notify_turn_delivery_failed(self, turn_id: str, reason: str) -> None:
        """terminal 事件未能送达 → 取消待提交的重启并恢复准入。"""
        logger.warning("control turn 终态未送达 turn=%s reason=%s", turn_id, reason)
        if self._restart_coordinator is not None:
            self._restart_coordinator.mark_delivery_failed(turn_id, reason)

    def start_thread(self, metadata: dict[str, Any]) -> dict[str, object]:
        thread_id = new_thread_id()
        session = self.sessions.get_or_create(thread_id, metadata=metadata)
        session.metadata.update(metadata)
        self.sessions.save(session)
        return self._thread_record(thread_id).to_dict()

    def resume_thread(self, thread_id: str) -> dict[str, object]:
        return self._thread_record(thread_id).to_dict()

    def list_threads(self, cursor: str | None, limit: int) -> dict[str, object]:
        rows = self.sessions.list_sessions()
        start = 0
        if cursor is not None:
            matching = [index for index, row in enumerate(rows) if row["key"] == cursor]
            if not matching:
                raise ThreadNotFoundError(f"thread cursor 不存在: {cursor}")
            start = matching[0] + 1
        page = rows[start : start + limit]
        threads = [self._thread_record(str(row["key"])).to_dict() for row in page]
        next_cursor = (
            str(page[-1]["key"]) if start + limit < len(rows) and page else None
        )
        return {"data": threads, "nextCursor": next_cursor}

    def read_thread(self, thread_id: str, include_turns: bool) -> dict[str, object]:
        payload = self._thread_record(thread_id).to_dict()
        if include_turns:
            payload["turns"] = [
                turn.to_dict() for turn in self.store.list_turns(thread_id, limit=200)
            ]
        return payload

    def delete_thread(self, thread_id: str) -> dict[str, object]:
        if self.runtime.is_thread_active(thread_id):
            raise ThreadBusyError(f"thread 正在执行: {thread_id}")
        if not self.sessions.delete_session(thread_id):
            raise ThreadNotFoundError(f"thread 不存在: {thread_id}")
        return {"id": thread_id, "deleted": True}

    async def start_turn(
        self, thread_id: str, input_text: str, metadata: dict[str, Any]
    ) -> TurnHandle:
        if not self.sessions.session_exists(thread_id):
            raise ThreadNotFoundError(f"thread 不存在: {thread_id}")
        return await self.runtime.start_turn(
            TurnRequest(thread_id, input_text, metadata)
        )

    def read_turn(self, thread_id: str, turn_id: str) -> dict[str, object]:
        return self.runtime.read_turn(thread_id, turn_id).to_dict()

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> dict[str, object]:
        return (await self.runtime.interrupt_turn(thread_id, turn_id)).to_dict()

    async def disable_and_drain_plugin(self, plugin_id: str) -> dict[str, object]:
        if self._plugin_drain is None:
            raise RuntimeError("当前 runtime 不支持插件 drain")
        message = await self._plugin_drain(plugin_id)
        return {"pluginId": plugin_id, "drained": True, "message": message}

    def start_consolidation(self, thread_id: str) -> OperationHandle:
        """启动 thread consolidation，并返回可独立观察的 operation。"""

        _ = self._thread_record(thread_id)
        if self._consolidate is None:
            raise RuntimeError("当前 runtime 不支持 consolidation")
        from plugins.control.ids import new_operation_id

        operation_id = new_operation_id()
        task = asyncio.create_task(
            self._run_consolidation(operation_id, thread_id),
            name=f"control-consolidation:{operation_id}",
        )
        self._operation_tasks.add(task)
        task.add_done_callback(self._operation_tasks.discard)
        return OperationHandle(operation_id, thread_id, task)

    async def shutdown(self) -> None:
        for task in self._operation_tasks:
            task.cancel()
        if self._operation_tasks:
            await asyncio.gather(*self._operation_tasks, return_exceptions=True)
        self._operation_tasks.clear()

    async def _run_consolidation(
        self,
        operation_id: str,
        thread_id: str,
    ) -> dict[str, object]:
        assert self._consolidate is not None
        try:
            changed = await self._consolidate(thread_id)
        except Exception as exc:
            return {
                "id": operation_id,
                "threadId": thread_id,
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        return {
            "id": operation_id,
            "threadId": thread_id,
            "status": "completed",
            "result": {"consolidated": changed},
        }

    def _thread_record(self, thread_id: str) -> ThreadRecord:
        meta = self.sessions.get_session_meta(thread_id)
        if meta is None:
            raise ThreadNotFoundError(f"thread 不存在: {thread_id}")
        source = (
            ThreadSource.PROGRAMMATIC
            if thread_id.startswith("programmatic:")
            else ThreadSource.CHANNEL
            if ":" in thread_id
            else ThreadSource.INTERNAL
        )
        created_at = datetime.fromisoformat(str(meta["created_at"]))
        updated_at = datetime.fromisoformat(str(meta["updated_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        else:
            created_at = created_at.astimezone(UTC)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        else:
            updated_at = updated_at.astimezone(UTC)
        return ThreadRecord(
            id=thread_id,
            source=source,
            created_at=created_at,
            updated_at=updated_at,
            metadata=dict(meta["metadata"] or {}),
        )


@dataclass(frozen=True)
class OperationHandle:
    id: str
    thread_id: str
    task: asyncio.Task[dict[str, object]]

    def record(self) -> dict[str, object]:
        return {"id": self.id, "threadId": self.thread_id, "status": "in_progress"}
