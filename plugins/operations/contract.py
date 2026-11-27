"""Operation Bus 公共契约：一个领域动作只有一个 owner，用户和 Agent 走同一个入口。

这是从上游 akashic 的 Self-CLI Operation Bus 移植的设计（上游 `plugins/operations`，
提交 `0af31c5c`）。采纳它的理由和本项目自己的主张是同一条：如果要说"Agent 做的事
和用户做的事留在同一个账本里、事后都说得出是谁做的"，那就不能让界面和 Agent 各自
走一条私有路径去改同一份状态。总线把"谁拥有这个动作"收敛成一处，`mirrors` 则让
**界面入口与 Agent 可调用操作的一对一关系变成可核对的门禁**，而不是一句说法。

业务插件通过可选子 Fiber 注入 `OPERATIONS_V1` 注册主操作或正交扩展选项，彼此零
import；`cli_node_key(path)` 复用 CompositionRoot 的拓扑激活序与热卸载级联清理。

**与上游的一处明确差异**：上游的 `ExtensionSpec` 还有 `stage`，让多个 owner 在同一
个联合事务里各自交出写入（上游提交 `928478b0` 为此在 ledger 加了 `OwnerWrite` 与多
owner 联合事务）。我们的 ledger 还没有那个原语，所以这里**不提供 `stage`**——留一个
不能真正原子提交的联合写入口，比没有更危险。扩展只有 `validate` 和 `apply`。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from agent.plugin_composition.context import Context
from agent.plugin_composition.effect import Effect
from agent.plugin_composition.model import ServiceKey

from plugins.ledger.contract import ContentPart

ActorKind = Literal["agent", "user"]
AudienceKind = Literal["agent_and_user", "user_only"]
OptionValueType = Literal["string", "bool", "int", "json"]


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """单个 CLI 参数声明（主操作裸名或正交扩展命名空间参数）。"""

    name: str
    description: str
    value_type: OptionValueType = "string"
    required: bool = False
    choices: tuple[str, ...] = ()
    default: str | bool | int | None = None
    set_once: bool = False


@dataclass(frozen=True, slots=True)
class OperationRequest:
    """规范化后的单次 Operation 调用上下文。"""

    operation_id: str
    actor: ActorKind
    session_id: str | None
    request_id: str | None
    args: Mapping[str, object]
    extensions: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OperationResult:
    """执行结果：机器可读 data，加给模型或终端看的简短文本与可选结构化片段。"""

    ok: bool
    summary: str
    data: Mapping[str, object] = field(default_factory=dict)
    parts: tuple[ContentPart, ...] = ()
    error_code: str | None = None


OperationHandler = Callable[[OperationRequest], Awaitable[OperationResult]]
ExtensionValidator = Callable[[OperationRequest, Mapping[str, object]], Awaitable[None]]
ExtensionApplier = Callable[
    [OperationRequest, OperationResult, Mapping[str, object]],
    Awaitable[Mapping[str, object] | None],
]


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """主操作注册声明；一个 operation 只有一个 owner。"""

    operation_id: str
    path: tuple[str, ...]
    summary: str
    handler: OperationHandler
    options: tuple[OptionSpec, ...] = ()
    extension_points: tuple[str, ...] = ()
    audience: AudienceKind = "agent_and_user"
    # 本操作对应的用户界面入口 ID，供一对一门禁核对；见 scripts/check_operation_parity.py。
    mirrors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtensionSpec:
    """正交扩展选项声明；按 target_operation 或 extension_point 挂载。"""

    namespace: str
    options: tuple[OptionSpec, ...]
    validate: ExtensionValidator | None = None
    apply: ExtensionApplier | None = None
    target_operation: str | None = None
    extension_point: str | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class CliNodeHandle:
    """主操作在 CompositionRoot 上发布的同构子树节点句柄。"""

    operation_id: str
    path: tuple[str, ...]


def cli_node_key(path: str) -> ServiceKey[CliNodeHandle]:
    """按点分路径返回同构子树 ServiceKey（如 `cli.node:project.create`）。"""
    return ServiceKey[CliNodeHandle](f"cli.node:{path}")


class Operations(Protocol):
    """统一注册、内省与执行总线。"""

    async def register(self, ctx: Context, spec: OperationSpec) -> Effect: ...

    async def extend(self, ctx: Context, spec: ExtensionSpec) -> Effect: ...

    def describe(self, path: tuple[str, ...] = (), *, actor: ActorKind = "agent") -> dict[str, object]: ...

    def render_help(self, path: tuple[str, ...] = (), *, actor: ActorKind = "agent") -> str: ...

    async def invoke(
        self, argv: list[str], *, actor: ActorKind,
        session_id: str | None = None, request_id: str | None = None,
    ) -> OperationResult: ...

    def receipt(self, request_id: str) -> OperationResult | None: ...

    def mirrors(self) -> Mapping[str, tuple[str, ...]]: ...


OPERATIONS_V1 = ServiceKey[Operations]("operations.v1")


class QueryHandler(Protocol):
    def __call__(
        self, method: str, payload: dict[str, object], *,
        session_id: str | None, turn_id: str | None,
    ) -> object | Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class QueryMirror:
    """把插件已有的一个界面查询方法原样发布为 operation。

    界面和 Agent 由此调用同一个领域入口，而不是两条各自维护的路径。
    """

    method: str
    path: tuple[str, ...]
    summary: str
    options: tuple[OptionSpec, ...] = ()
    audience: AudienceKind = "agent_and_user"
    entry: str = ""


def _query_handler(query: QueryHandler, mirror: QueryMirror) -> OperationHandler:
    async def handle(request: OperationRequest) -> OperationResult:
        payload = {name: request.args[name] for name in (o.name for o in mirror.options)
                   if name in request.args}
        value = query(mirror.method, payload, session_id=request.session_id, turn_id=None)
        if isinstance(value, Awaitable):
            value = await value
        data = value if isinstance(value, Mapping) else {"value": value}
        return OperationResult(True, mirror.summary, data=dict(data))

    return handle


async def register_query_mirrors(
    ctx: Context, operations: Operations, *, query: QueryHandler,
    mirrors: Sequence[QueryMirror],
) -> None:
    """逐个注册镜像 operation；参数名即界面载荷键，入口 ID 默认 `rpc:<插件>/<method>`。"""
    # 运行期身份带来源后缀，operation id 只用插件名，与门禁采集一致。
    plugin = ctx.runtime.plugin_id.split("@", 1)[0]
    for mirror in mirrors:
        _ = await operations.register(ctx, OperationSpec(
            operation_id=f"{plugin}.{mirror.method}",
            path=mirror.path,
            summary=mirror.summary,
            handler=_query_handler(query, mirror),
            options=mirror.options,
            audience=mirror.audience,
            mirrors=(mirror.entry or f"rpc:{plugin}/{mirror.method}",),
        ))
