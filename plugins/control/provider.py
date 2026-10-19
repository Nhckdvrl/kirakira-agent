from agent.composition.lifecycle import on_close, on_start, on_stopping

"""Declare this owner's dependencies, capabilities and resource lifetime."""

import os

from plugins.control.contract import CONTROL
from plugins.control.factory import build_control_plane
from plugins.conversation.contract import CONVERSATION
from plugins.default_memory.contract import MEMORY_SERVICES
from plugins.extensions.contract import EXTENSIONS
from plugins.ledger.contract import SESSIONS
from plugins.projects.contract import WORKSPACE
from plugins.runtime_restart.contract import RESTART

inject = (CONVERSATION, SESSIONS, WORKSPACE, MEMORY_SERVICES, EXTENSIONS, RESTART)
provides = (CONTROL,)


async def apply(ctx):
    workdir = ctx.config["workspace"]
    pipeline = ctx.get(CONVERSATION)
    session_manager = ctx.get(SESSIONS)
    workspace_services = ctx.get(WORKSPACE)
    memory_services = ctx.get(MEMORY_SERVICES)
    plugin_manager = ctx.get(EXTENSIONS)
    restart = ctx.get(RESTART)
    commit_channel, restart_coordinator = restart

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

    control_store, control_runtime, control_service, control_server = (
        build_control_plane(
            workspace=workdir,
            pipeline=pipeline,
            sessions=session_manager,
            workspace_services=workspace_services,
            endpoint=os.getenv("KIRAKIRA_CONTROL_ENDPOINT", "").strip() or None,
            workspace_token=os.getenv("KIRAKIRA_CONTROL_TOKEN", "").strip() or None,
            boot_id=commit_channel.boot_id if commit_channel is not None else None,
            plugin_drain=plugin_manager.reconcile_disabled_and_drain,
            consolidate=_control_consolidate,
            restart_coordinator=restart_coordinator,
        )
    )
    await on_close(ctx, lambda: control_store.close())
    await on_close(ctx, lambda: control_runtime.shutdown())
    await on_close(ctx, lambda: control_service.shutdown())
    await on_close(ctx, lambda: control_server.stop())

    await ctx.provide(
        CONTROL, (control_store, control_runtime, control_service, control_server)
    )

    await on_start(ctx, control_server.start)
    await on_stopping(ctx, control_server.stop)
    await on_stopping(ctx, control_service.shutdown)
    await on_stopping(ctx, control_runtime.shutdown)
