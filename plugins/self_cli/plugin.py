"""模型工具 `self`：把 argv 交给 Operation Bus。

和 `operations` 分开成两个插件，是为了避免 operations 依赖工具目录而成环——总线处在
依赖图底层，谁都能往上挂，它自己不认识工具。

这个插件是"用户和 Agent 走同一张操作表"这句话落地的地方，两条入口在这里汇合：

* 模型工具 `self` → `actor="agent"`
* RPC 方法 `self/invoke` → `actor="user"`

**差别只有 `actor`**，调的是同一个 `OPERATIONS_V1.invoke`。所以 `audience="user_only"`
的操作天然只在后一条可用，不需要在两套实现里各写一遍权限判断——那正是两条私有路径
最容易写歪的地方。

幂等性保守声明为非幂等：这是一个读写混合的单工具。具体写操作的重入由总线的回执按
`request_id` 恢复，而工具的 `key` 就是那个 `request_id`，所以同一次工具调用重试不会
把副作用做第二遍。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.plugin_composition import Context
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import ContentPart, json_value
from plugins.operations.contract import OPERATIONS_V1, OperationResult, Operations
from plugins.tools.contract import TOOLS, CallSource, Result

api_version = 3
name = "self_cli"
version = "1.0.0"
desc = "模型工具 self：通过统一内省式 CLI 调用本体能力"
inject = ()

INVOKE_METHOD = "self/invoke"


class SelfCliInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: list[str] = Field(
        max_length=256,
        description='命令行参数数组，例如 ["help"]、["describe"] 或 ["wd", "current"]。',
    )


class SelfCliTool:
    idempotent = False

    def __init__(self, registry: Operations) -> None:
        self._registry = registry

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None,
    ) -> Mapping[str, object] | str:
        try:
            parsed = SelfCliInput.model_validate(json_value(arguments))
        except ValidationError as err:
            return str(err)
        session_id = source.messages[-1].session_id if source and source.messages else None
        return {"argv": parsed.argv, "session_id": session_id}

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        argv = [str(item) for item in cast(list[object], arguments.get("argv", []))]
        session_raw = arguments.get("session_id")
        session_id = session_raw if isinstance(session_raw, str) else None
        # The tool call key is the request id, so a retried call replays the
        # bus receipt instead of performing the write twice.
        result = await self._registry.invoke(
            argv, actor="agent", session_id=session_id, request_id=key)
        return _tool_result(result)

    async def query(self, key: str) -> Result | None:
        saved = self._registry.receipt(key)
        return None if saved is None else _tool_result(saved)


def _tool_result(result: OperationResult) -> Result:
    text = result.summary
    if result.data and not text.lstrip().startswith("{"):
        text = f"{text}\n{json.dumps(json_value(dict(result.data)), ensure_ascii=False)}"
    return Result("success" if result.ok else "error",
                  (ContentPart("text", text), *result.parts))


class SelfInvokeParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: list[str] = Field(max_length=256)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=512)


async def apply(ctx: Context) -> None:
    """工具目录与总线都在时暴露 `self`；总线在时暴露用户侧 RPC 入口。"""

    async def register_tool(child: Context) -> None:
        tools = child.require(TOOLS)
        _ = await tools.declare_group(child, always_on=True, description=desc)

        @asynccontextmanager
        async def open_tool(_state: Mapping[str, object]) -> AsyncIterator[SelfCliTool]:
            yield SelfCliTool(child.require(OPERATIONS_V1))

        _ = await tools.register(
            child,
            name="self",
            description=('通过统一内省式 CLI 调用本体能力；传 ["help"] 查看当前'
                         "已注册的命令与正交扩展选项，再按 help 的提示调用。"),
            parameters=SelfCliInput.model_json_schema(),
            open=open_tool,
            idempotent=False,
        )

    async def register_shell(child: Context) -> None:
        registry = child.require(OPERATIONS_V1)

        async def invoke(params: BaseModel) -> dict[str, object]:
            call = cast(SelfInvokeParams, params)
            result = await registry.invoke(
                call.argv, actor="user", session_id=call.session_id,
                request_id=call.request_id)
            return {
                "ok": result.ok, "summary": result.summary,
                "data": json_value(dict(result.data)), "error_code": result.error_code,
                "parts": [json_value({"kind": part.kind, "value": part.value})
                          for part in result.parts],
            }

        _ = await child.provide(RpcMethod.key(INVOKE_METHOD),
                                RpcMethod(SelfInvokeParams, child.entrypoint(invoke)))

    _ = await ctx.inject((TOOLS, OPERATIONS_V1), register_tool, name="self-cli-tool")
    _ = await ctx.inject((OPERATIONS_V1,), register_shell, name="self-cli-shell")
