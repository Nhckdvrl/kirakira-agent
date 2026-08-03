# Proactive / Drift 架构

当前主动系统由 `proactive_v2/` 内核、`plugins/wake_proactive/` 状态与数据源、
`plugins/proactive_flow/` 判断模块、`plugins/drift_flow/` 空闲任务组成。

## 拓扑

```text
CoreRuntime.start_background
  └─ ProactiveLoop.run
      └─ tick frame（锁定 plugin generation + runtime snapshot）
          ├─ gate
          ├─ fetch sources
          ├─ ingest/deduplicate
          ├─ resolve alert/content/context
          ├─ deliver 或 drift
          └─ commit + feedback + next wakeup
```

模块顺序由 `requires/produces` 依赖图编译，不依赖文件行号；terminal module 可以提前结束本轮。
tick 与每个 step 的开始、完成、耗时和错误都持久化，因此 UI 或控制面可以还原运行轨迹。

## 与被动链共享什么

- 同一个 MessageBus 和真实 Channel sender；
- 同一个 Session/MemoryServices；
- 同一个 model provider；
- 同一个插件代际与能力快照机制。

主动判断不是普通用户 turn：它有自己的 Gate、冷却、去重和提交边界。Scheduler 的 soft turn 也使用
隔离 `scheduler:<job-id>` session，不能混进用户对话。

## 状态提交边界

```text
source event
  → 本地 ingest（稳定 item_id）
  → decision
  → Channel DeliveryReceipt.success
  → Session + consume + delivery dedup
  → source ACK / interesting feedback
```

任何投递失败都不能提前 consume。content 只消费模型实际引用的 cited ids；没引用的候选保留到后续
tick 或过龄淘汰。反馈发送失败写入 pending，后续 Gate 先刷新。

## 轨迹与可视化基础

- 普通 Agent：assistant message 的 `tool_chain`、`context_trace`；控制面 turn/tool/stream events。
- Proactive：`tick_log`、`tick_step_log`、`decisions`。
- Drift：`drift.db` run/step/message result 与 continuity。

所以后端已经能回答“Agent 做了哪些工具调用、上下文为何降级、主动 tick 停在哪一步”。当前没有新增
轨迹可视化页面，是前端范围延期，不是数据缺失。

## 仍可加厚

日配额、累计 hazard、兴趣 embedding、更多 feedback 类型和跨系统 durable outbox 可以以后在现有
Gate/state/source 边界内增加。当前合同与操作方法见
[_handbook/proactive.md](../_handbook/proactive.md)，模块化重构依据见
[design/proactive-lifecycle.md](./design/proactive-lifecycle.md)。
