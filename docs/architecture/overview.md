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

Scheduler soft turn ───────────────┐
Proactive tick → Deliver/Drift ────┴→ 使用相同 Runtime 边界，但使用隔离 Session/提交语义
```

## 目录 owner

| 目录 | 负责什么 |
| --- | --- |
| `agent/` | 推理循环、上下文、工具、MCP、插件、调度、子 Agent、控制协议 |
| `bootstrap/` | 组合根、CLI/setup、supervisor、控制面和 dashboard 装配 |
| `bus/` | 入站/出站队列和 lifecycle 事件 |
| `core/` | 共享 schema、网络和记忆协议/runtime |
| `infra/` | provider、Channel、控制连接和持久化 adapter |
| `session/` | Session 与消息 embedding 的权威存储 |
| `memory2/` | Default Memory 的算法和结构化存储 |
| `plugins/` | Default/Akasha、Proactive、Drift 等一方实现 |
| `plugin_packages/` | 通过公开插件 API 分发的插件包 |
| `proactive_v2/` | 主动链的 frame、模块合同和 tick 编排 |
| `frontend/` | TUI 和本地 Web 表现层 |
| `eval/` | 记忆评测工具 |
| `migrations/` | append-only workspace migration |
| `kirakira_agent/` | 只保留 `python -m kirakira_agent` 入口 |

依赖方向是“外层装配依赖内层合同”：`bootstrap` 可以组合所有 owner；`core` 不应反向依赖
`bootstrap` 或具体 Channel。具体 provider、Channel 和存储通过 `infra` adapter 接入。

## 主要持久状态

| 文件 | owner | 作用 |
| --- | --- | --- |
| `sessions.db` | `SessionManager` | Session 和消息真相 |
| `memory/coremem.db` | Default Memory | 结构化长期记忆 |
| `memory/akasha.db` | Akasha | 图、激活和查询状态 |
| `proactive.db` | Proactive state | 事件、决策、ACK、feedback、tick trace |
| `drift/drift.db` | Drift state | run、journal、continuity、hazard schedule |
| `.kirakira/control.db` | ControlStore | programmatic turn 状态 |
| `migrations.sqlite3` | Yoyo | workspace migration ledger |

## 四条关键不变量

1. Session 历史与模型上下文投影分离；context limit 不能删除历史。
2. 热更新只切换新 turn 的 current snapshot；在途 turn 继续使用原代际。
3. 外部副作用以真实 delivery/tool result 为提交边界，不能先写“成功”。
4. `Reference/` 只用于开发审计，不能成为运行依赖。

从 [当前状态](../status/current.md) 继续了解已完成范围，或按
[文档索引](../INDEX.md) 进入具体子系统。
