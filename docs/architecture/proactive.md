# 主动执行架构

Proactive 与被动 AgentLoop 并列运行，但复用模型、插件快照、Channel 和 Session 提交边界。主循环只做
调度；业务步骤由模块 DAG 组成，插件可以声明新 source 或插入模块。

## Tick 拓扑

```text
Gate
  → Fetch
  → Ingest
  → DecideAlert / DecideContent
  → Deliver
  → Drift（仅本轮没有投递时）
  → Commit
```

模块通过 `slot`、`requires` 和 `before/after` 形成依赖图。缺依赖、循环或初始化失败会在编译阶段暴露，
不会运行到一半才发现拓扑无效。

## Frame 与终态

每个 tick 使用独立 frame，携带候选事件、context、decision、pending side effect 和 trace。每个 module
都记录 started/completed/skipped/failed 与耗时。terminal 明确说明本轮停止原因，例如 gated、delivered、
no-candidate 或 error。

source fetch 抛错会进入 step/tick error，不会伪装成“没有内容”。alert 判断失败可以退回原文；content
判断失败则安全跳过，避免误打扰。

## 快照和租约

tick 开始时固定插件 generation、工具和 MCP snapshot。在途 tick 不受热重载影响；新 source 或模块从
下一轮开始可见。旧 generation 在所有被动 turn 和主动 tick 都释放租约后才关闭。

## 投递提交

```text
mark delivery fingerprint
  → Channel dispatch
  → success: 写 Session、consume、cooldown、ACK/feedback
  → failure: 撤销 fingerprint，保留候选供重试
```

若进程在 mark 后、发送前崩溃，可能漏发；若在发送后、本地提交前崩溃，持久 fingerprint 会抑制重复。
这是针对主动消息选择的“窗口内至多一次”，不是 exactly-once。理由见[决策 0004](../decisions/0004-delivery-dedup.md)。

## 状态与观测

`proactive.db` 持有 decision、tick、step、delivery、pending ACK/feedback 和部分 source 状态。每个 tick
及步骤的 terminal、耗时和错误均可查询，因此后续可视化无需重新解析日志。

Drift 运行记录和 hazard schedule 位于 `drift/drift.db`。hazard 使用采样到期，避免轮询频率改变触发
概率，见[决策 0005](../decisions/0005-drift-hazard-sampled-expiry.md)。

## 使用入口

配置、事件类型和排查方式见[主动执行手册](../handbook/proactive.md)；Drift 见
[Drift 手册](../handbook/drift.md)。
