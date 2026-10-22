from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from plugins.ledger.contract import ContentPart, Output, ToolCall, ToolResult
from plugins.ledger.contract import json_value

from ._boundary import CallSource, ToolResultValue
from .request import STAGE_TOOLS, read_phase, read_request
from .content import _candidate_payloads
from .selection import propose_content


class ScreenedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: str = Field(min_length=1, description="逐字复制本轮候选的完整 candidate_id，不得截短、改写或自行生成。")
    initial_interest: str = Field(min_length=1, description="初步值得调查的理由；这是兴趣假设，不是已核实结论。")
    question: str = Field(min_length=1, description="调查阶段需要核实的问题，包括用户当前条件和历史更正。")


class Screen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[ScreenedItem] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({item.candidate_id for item in self.items}) != len(self.items):
            raise ValueError("初筛不能重复同一个候选")
        return self


class Share(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1)
    items: list[str] = Field(max_length=5)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.items)) != len(self.items):
            raise ValueError("分享不能重复同一个候选")
        return self


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1)


class Skip(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1)


SCHEMAS: dict[str, type[BaseModel]] = {
    "screen_content": Screen, "share_content": Share, "share_alert": Alert, "skip_content": Skip,
}


class DecisionTool:
    """只在真实 Wake 调用里记录结构化决定；发送和领域确认仍归来源处理。"""

    idempotent = True

    def __init__(self, name: str):
        self.name = name

    async def prepare(self, arguments: Mapping[str, object], source: CallSource | None = None) -> Mapping[str, object] | str:
        if source is None:
            raise PermissionError('Wake 决策只能由内部消息程序调用')
        try:
            request = read_request(source.messages)
            phase_message, phase = read_phase(source.messages, request)
        except ValueError as error:
            raise PermissionError('Wake 决策缺少真实内部请求') from error
        message = next(message for message in source.messages if message.message_id == source.call_ref.message_id)
        if message.source != "wake" or not isinstance(message.body, Output):
            raise PermissionError('当前来源无权提交 Wake 决策')
        call = message.body.parts[source.call_ref.part_index]
        if not isinstance(call, ToolCall) or self.name not in STAGE_TOOLS[phase.stage] or call.binding_id != request.tools.get(self.name):
            raise PermissionError('当前阶段未授予此 Wake 工具')
        try:
            result = SCHEMAS[self.name].model_validate(json_value(arguments))
        except ValidationError as error:
            return str(error)
        if isinstance(result, (Share, Skip)) and request.history_check_required:
            by_id = {item.message_id: item for item in source.messages}
            checked, unavailable = False, request.tools.get("recall_memory") is None
            for item in source.messages:
                read = item.body
                if (item.seq <= phase_message.seq or item.seq >= message.seq or item.source != "wake"
                        or not isinstance(read, ToolResult)):
                    continue
                parent = by_id.get(read.call_ref.message_id)
                if parent is None or parent.seq <= phase_message.seq or not isinstance(parent.body, Output):
                    continue
                memory_call = parent.body.parts[read.call_ref.part_index]
                if isinstance(memory_call, ToolCall) and memory_call.binding_id == request.tools.get("recall_memory"):
                    unavailable |= read.outcome != "success"
                    if read.outcome == "success" and any(part.kind == "akasha.recall" for part in read.parts):
                        checked = True
                        break
            if not checked and not (isinstance(result, Skip) and unavailable):
                return ("该跟进绑定了原始对话，作出分享或跳过决定前必须在本次调查中成功调用 recall_memory，"
                        "检查该事项的最新安排、更正和约束。最初关注依据不是当前安排。"
                        "原文较长时可使用 presentation='excerpts' 并以 read_memory_source 核对。"
                        "没有记忆读取能力或无法核实时，请用 skip_content 说明缺少的依据。")
        if isinstance(result, Screen):
            # 候选身份在模型输入边界核对，错误反馈给本轮模型纠正。
            proposal = propose_content(request.items, now=request.now)
            if proposal is None:
                raise RuntimeError("Wake 初筛缺少原候选集合")
            allowed = {item["candidate_id"] for item in _candidate_payloads(proposal)}
            unknown = [item.candidate_id for item in result.items if item.candidate_id not in allowed]
            if unknown:
                return f'candidate_id 不属于本轮候选: {unknown}。请逐字复制候选中的完整 ID，不得截短；本次决定未记录。'
        return result.model_dump(mode="json")

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> ToolResultValue:
        return ToolResultValue("success", (ContentPart("text", "Wake 决定已记录。"),))

    async def query(self, key: str) -> ToolResultValue | None:
        return None
