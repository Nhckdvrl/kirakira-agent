"""Expose supervised restart as a deferred tool."""

from typing import Any


def register_agent_restart_tool(registry: Any, coordinator: Any) -> None:
    """注册 agent_restart(照 Reference agent/tools/agent_restart.py)。

    只在 supervisor 托管时注册;deferred 对应 Reference 的 requires_turn_search 边界
    (模型必须先 tool_search 发现它,不进默认工具面)。turn 上下文取自 ContextVar 与
    registry context;非控制面 turn 没有 current_turn_id,arm 会明确拒绝而不是挂起。
    """
    import json as _json

    from core.schema import ToolSpec
    from plugins.control.context import current_turn_id
    from plugins.tools.registry import object_schema

    async def agent_restart(reason: str) -> str:
        ctx = registry.context
        request = coordinator.arm(
            turn_id=current_turn_id.get(),
            session_key=str(ctx.get("session_key") or ""),
            channel=str(ctx.get("channel") or ""),
            chat_id=str(ctx.get("chat_id") or ""),
            reason=reason,
        )
        return _json.dumps(
            {
                "status": "scheduled",
                "requestId": request.id,
                "message": "将在本轮回复持久化并送达后重启。",
            },
            ensure_ascii=False,
        )

    registry.register(
        ToolSpec(
            "agent_restart",
            "安全重启当前 Kirakira Agent。仅在核心代码或主配置必须重新加载时使用；"
            "MCP 与插件可热重载时不要调用。执行后会等待本轮回复持久化并送达。",
            object_schema(
                {
                    "reason": {
                        "type": "string",
                        "description": "需要完整重启 Agent 的具体原因",
                        "minLength": 1,
                        "maxLength": 300,
                    }
                },
                ["reason"],
            ),
        ),
        agent_restart,
        deferred=True,
    )
