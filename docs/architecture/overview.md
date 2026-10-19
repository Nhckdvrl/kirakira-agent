# 架构总览

Kirakira 是一个本地优先的多渠道 Agent Runtime。它有三条运行链，但共享同一套 Session、工具、
记忆、插件和消息总线。

```text
Channel/CLI
  → MessageBus
  → AgentLoop
  → PassiveTurnPipeline
  → Context render
  → ReAct + tools
  → Session commit
  → Channel delivery

Scheduler run → 原 Session admission → 同一 Pipeline → 来源标记提交 → 投递回执
Scheduler notify → 原 Session admission → 原样消息提交 → 投递回执
Proactive tick → scoped Wake/Drift → 共享 reasoner 与各自提交语义
```

## 执行职责

`runtime.py` 只负责装配后的生命周期与直接入口；`loop.py` 负责渠道调度。
`pipeline.py` 负责对话提交与记忆边界，`admission.py` 为渠道、直接和控制调用统一排队与取消。
`conversation.py` 构造上下文，`reasoner.py` 执行共享循环，`model_io.py` 与 `tool_io.py` 拥有 I/O。
scoped Wake/Drift 与子任务都复用该循环。`execution.py` 保存执行事实和不确定结果，
不把执行完成等同于渠道送达。恢复限制见 [ADR0008](../decisions/0008-core-execution-and-admission.md)。

`tool_batch.py` 为提供者显式声明的独立读取提供有界并发，写操作与工具加载/终结操作保持屏障，
结果按模型顺序进入上下文。准入层拥有全部入口的在途与等待任务；取消排空、关闭顺序与中断控制
提示见 [ADR0014](../decisions/0014-main-chain-batches-and-physical-drain.md)。

## 目录 owner

| 目录 | 负责什么 |
| --- | --- |
| `agent/composition/` | 有版本能力、代际借用与资源 scope |
| `agent/` | 推理循环、上下文、工具、MCP、插件、子 Agent、控制协议 |
| `bootstrap/` | 组合根、CLI/setup、supervisor、控制面和 dashboard 装配 |
| `bus/` | 入站/出站队列和 lifecycle 事件 |
| `core/` | 共享 schema、网络和记忆协议/runtime |
| `infra/` | provider、Channel、控制连接和持久化 adapter |
| `session/` | Session 与消息 embedding 的权威存储 |
| `memory2/` | Default Memory 的算法和结构化存储 |
| `plugins/` | Default/Akasha、Proactive、Drift、Scheduler 等一方实现 |
| `plugin_packages/` | 通过公开插件 API 分发的插件包 |
| `proactive_v2/` | 主动链的 frame、模块合同和 tick 编排 |
| `frontend/` | TUI 和本地 Web 表现层 |
| `eval/` | 记忆评测工具 |
| `migrations/` | append-only workspace migration |
| `kirakira_agent/` | 只保留 `python -m kirakira_agent` 入口 |

依赖方向是“外层装配依赖内层合同”：`bootstrap` 可以组合所有 owner；`core` 不应反向依赖
`bootstrap` 或具体 Channel。具体 provider、Channel 和存储通过 `infra` adapter 接入。

## 装配与工具提供者

`bootstrap/runtime.py` 负责构造、失败回收和所有权交接；`bootstrap/app.py` 负责命令入口。
渠道与主动链路有各自的装配模块。Runtime 与 ToolRegistry 共用 ResourceScope 的物理关闭边界，
关闭失败在所有动作尝试后聚合报告。

工作区、网页与记忆工具分别由 `agent/tools/builtins.py`、`agent/tools/web.py` 和
`plugins/memory_tools/tools.py` 实现，完整对话工具面由 `bootstrap/tools.py` 组合。
插件公共类型在 `agent/plugins/api.py`，管理器负责加载与代际操作。
详见 [ADR0010](../decisions/0010-provider-ownership-and-runtime-disposal.md)。

## 主要持久状态

| 文件 | owner | 作用 |
| --- | --- | --- |
| `sessions.db` | `SessionManager` | Session、消息真相和只追加的 context compaction ledger |
| `memory/coremem.db` | Default Memory | 结构化长期记忆 |
| `memory/akasha.db` | Akasha | v1 图、激活和查询状态 |
| `memory/akasha2.db` | Akasha2 | 图、消费位置、向量空间与原始消息绑定的原子快照 |
| `proactive.db` | Proactive state | 事件、决策、ACK、feedback、tick trace |
| `drift/drift.db` | Drift state | proposal、run、journal、continuity、hazard schedule |
| `.kirakira/executions.db` | `ExecutionStore` | 模型/工具事实、终态与待核对调用；不自动重放 |
| `.kirakira/control.db` | ControlStore | programmatic turn 状态 |
| `migrations.sqlite3` | Yoyo | workspace migration ledger |

## 四条关键不变量

1. Session 历史与模型上下文投影分离；context limit 只能推进 summary ledger，不能删除历史。
2. 热更新只切换新 turn 的 current snapshot；在途 turn 继续使用原代际。
3. 外部副作用以真实 delivery/tool result 为提交边界，不能先写“成功”。
4. `Reference/` 只用于开发审计，不能成为运行依赖。

从 [当前状态](../status/current.md) 继续了解已完成范围，或按
[文档索引](../INDEX.md) 进入具体子系统。

Scheduler 入口和故障边界见 [ADR0011](../decisions/0011-source-session-scheduler.md)。
