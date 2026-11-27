"""Operation Bus 插件入口：提供 OPERATIONS_V1。

只依赖 ledger 契约，处在插件依赖图底层，所以任何业务插件都能软挂载 operation 而
不成环。模型可调用的 CLI 入口由上层插件挂到工具目录，本插件不注册工具。
"""
from __future__ import annotations

from agent.plugin_composition import Context

from .contract import OPERATIONS_V1
from .registry import OperationRegistry

api_version = 3
name = "operations"
version = "1.0.0"
desc = "Operation Bus 与正交扩展注册中心：一个领域动作一个 owner，用户与 Agent 同一入口"
inject = ()


async def apply(ctx: Context) -> None:
    """发布 OPERATIONS_V1。"""
    _ = await ctx.provide(OPERATIONS_V1, OperationRegistry())
