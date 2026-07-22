"""Drift run 的收尾工具：``message_push`` 与 ``finish_drift``。

两个工具都只改写本轮的 ``DriftRunContext``（同步、无副作用外泄）：
- ``message_push`` 记录一条草稿消息（fire-and-forget，最多一次），
  真正的投递由 runner 在 agent run 结束后到主事件循环上完成。
- ``finish_drift`` 记录 status / briefing / 连续性，标记本轮结束。

这样设计避免了在工作线程里跨事件循环访问 async 的 MessageBus。
参考 akashic 的 `plugins/wake_proactive/tools.py` 收尾语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kirakira_agent.schema import ToolSpec
from kirakira_agent.tools.registry import ToolRegistry, object_schema


@dataclass
class DriftRunContext:
    skill: str
    draft_message: str = ""
    message_pushed: bool = False
    finished: bool = False
    status: str = "completed"
    briefing: str = ""
    scratchpad_update: str = ""
    next_tendency: str = ""


def register_drift_tools(registry: ToolRegistry, ctx: DriftRunContext) -> None:
    def message_push(message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return "Error: message is empty"
        if ctx.message_pushed:
            return "Error: message_push 本轮只能调用一次"
        ctx.draft_message = text
        ctx.message_pushed = True
        return "已记录待发送消息（将在本轮结束后投递）。"

    def finish_drift(
        status: str = "completed",
        briefing: str = "",
        scratchpad_update: str = "",
        next_tendency: str = "",
    ) -> str:
        normalized = str(status or "").strip().lower()
        if normalized not in ("completed", "paused"):
            return "Error: status 必须是 completed 或 paused"
        if normalized == "paused" and not str(scratchpad_update or "").strip():
            return "Error: paused 时必须填写 scratchpad_update 说明下次从哪继续"
        ctx.finished = True
        ctx.status = normalized
        ctx.briefing = str(briefing or "").strip()
        ctx.scratchpad_update = str(scratchpad_update or "").strip()
        ctx.next_tendency = str(next_tendency or "").strip()
        return "Drift 本轮已收尾（status=%s）。不要再调用任何工具。" % normalized

    # 覆盖内置的 async message_push：drift run 跑在工作线程里，不能跨事件循环直连 bus，
    # 所以这里改成同步记录草稿，真正投递交给 runner 在主循环上完成。
    if registry.has("message_push"):
        registry.unregister("message_push")
    registry.register(
        ToolSpec(
            "message_push",
            "主动给用户推一条消息（fire-and-forget，本轮最多一次）。",
            object_schema({"message": {"type": "string"}}, ["message"]),
        ),
        message_push,
    )
    registry.register(
        ToolSpec(
            "finish_drift",
            "保存本轮 Drift 的状态并结束。执行完毕前必须调用。",
            object_schema(
                {
                    "status": {"type": "string", "enum": ["completed", "paused"]},
                    "briefing": {"type": "string"},
                    "scratchpad_update": {"type": "string"},
                    "next_tendency": {"type": "string"},
                },
                ["status", "briefing"],
            ),
        ),
        finish_drift,
    )
