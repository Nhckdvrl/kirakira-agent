"""把控制面接到 kirakira 的被动链路上。

Reference 在 `bootstrap/` 里完成同样的接线:控制面自己不知道 agent 怎么跑,
只拿到一个 ``TurnExecutor``。这里提供那个 executor,以及一次性装配好整套
控制面的 ``build_control_plane``。

**thread 与 session 的关系**:控制面 thread id 形如 ``programmatic:<uuid>``,
与渠道 session key(``telegram:123``)天然不同名,因此控制面 turn 与渠道 turn
不会落到同一个 session 上。控制面 turn 直接调 pipeline,不经过 MessageBus,
串行由 ``ConversationRuntime`` 的"每 thread 至多一个 active turn"保证。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from kirakira_agent.control.ids import new_item_id
from kirakira_agent.control.models import (
    TurnItem,
    TurnItemKind,
    TurnRequest,
    TurnUsage,
)
from kirakira_agent.control.ports import ControlExecutionResult
from kirakira_agent.control.runtime import ConversationRuntime
from kirakira_agent.control.service import ControlService
from kirakira_agent.control.socket import SocketAppServer
from kirakira_agent.control.store import ControlStore
from kirakira_agent.events import InboundMessage

logger = logging.getLogger(__name__)

CONTROL_CHANNEL = "control"


def build_turn_executor(pipeline: Any) -> Callable[[TurnRequest], Any]:
    """返回一个把控制面 turn 投影到被动 pipeline 的 executor。"""

    async def execute(request: TurnRequest) -> ControlExecutionResult:
        msg = InboundMessage(
            channel=CONTROL_CHANNEL,
            sender="control",
            chat_id=request.thread_id,
            content=request.input,
            metadata={
                "session_key_override": request.thread_id,
                # 控制面自己负责把结果回给调用方,不要再走 bus 出站。
                **{
                    key: value
                    for key, value in request.metadata.items()
                    # 内部回调不是模型可见的输入,不进 metadata。
                    if not key.startswith("_")
                },
            },
        )
        outbound = await pipeline.run(msg, request.thread_id, dispatch_outbound=False)

        # 工具调用投影成 toolCall item,让 thread/read 能看到本轮做了什么。
        items: list[TurnItem] = []
        for group in outbound.metadata.get("tool_chain") or []:
            for call in group.get("calls") or []:
                items.append(
                    TurnItem(
                        TurnItemKind.TOOL_CALL,
                        new_item_id(),
                        {
                            "name": call.get("name", ""),
                            "arguments": call.get("arguments", {}),
                            "result": str(call.get("result", ""))[:4000],
                            "status": call.get("status", ""),
                        },
                    )
                )
        if outbound.thinking:
            items.append(
                TurnItem(
                    TurnItemKind.REASONING,
                    new_item_id(),
                    {"content": outbound.thinking},
                )
            )

        assistant_data: dict[str, object] = {}
        attention = outbound.metadata.get("mobile_attention")
        if attention is not None:
            # 本轮有工具声明"需要用户确认",一路带到客户端。
            assistant_data["mobileAttention"] = attention

        return ControlExecutionResult(
            response=outbound.content,
            items=items,
            usage=_usage_from_metadata(outbound.metadata),
            assistant_data=assistant_data,
        )

    return execute


def _usage_from_metadata(metadata: dict[str, Any]) -> TurnUsage | None:
    """从 turn trace 里取 token 统计;拿不到就明确标 unavailable。"""
    trace = metadata.get("context_retry") or {}
    stats = trace.get("react_stats") if isinstance(trace, dict) else None
    if not isinstance(stats, dict):
        return None
    requests = stats.get("request_count")
    if not isinstance(requests, int) or isinstance(requests, bool):
        return None
    return TurnUsage(request_count=requests, coverage="partial")


def build_control_plane(
    *,
    workspace: Path,
    pipeline: Any,
    sessions: Any,
    endpoint: str | Path | None = None,
    workspace_token: str | None = None,
    boot_id: str | None = None,
    plugin_drain: Callable[[str], Any] | None = None,
    consolidate: Callable[[str], Any] | None = None,
) -> tuple[ControlStore, ConversationRuntime, ControlService, SocketAppServer]:
    """装配 store → runtime → service → socket 四层并返回,调用方负责 start/stop。"""
    store = ControlStore(Path(workspace) / ".kirakira" / "control.db")
    runtime = ConversationRuntime(store, build_turn_executor(pipeline))
    service = ControlService(
        runtime,
        sessions,
        store,
        Path(workspace),
        plugin_drain=plugin_drain,
        consolidate=consolidate,
        workspace_token=workspace_token,
        boot_id=boot_id,
        ready=lambda: True,
    )
    resolved = endpoint or (Path(workspace) / ".kirakira" / "control.sock")
    server = SocketAppServer(resolved, service)
    return store, runtime, service, server
