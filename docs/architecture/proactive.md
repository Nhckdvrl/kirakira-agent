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

模块抛错进入 step/tick error。当前 SourceRegistry 的单源抓取异常会记录日志并返回空列表，
不等于已有结构化 source error 状态。Alert 判断失败可以退回原文；Content 判断失败保留候选，
按持久 retry_at 延后重试。

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

## 共享执行与决策权限（2026-10）

被动对话、ProactiveJudge、DriftRunner 共用 `DefaultReasoner.run`。后台通过 `run_scoped` 传入冻结输入、
工具授权、步数、终结工具；不把内部判别过程写成用户对话。scoped 自身不负责持久程序恢复或上下文压缩。

Content 只允许 `share_content(message, items)` / `skip_content(reason)`；Alert 只允许 `share_alert`。
引用必须是本轮候选中 1–5 个不重复的 ID，未知 ID 或冲突终结批次整体失败。普通回复即使长得像 JSON
也没有投递权限。只有 handler 成功后才结束循环，owner 在循环外负责渠道提交。

`content_checks` 记录 checked/retry 状态；`decision_runs` 保存输入候选、prompt、模型和工具轨迹。
冷却不消费待检查资格，有效 skip 不被每次 tick 重判，非法决策/模型错误/投递失败延后 300 秒重试。
发送前先保存 delivery_pending，因此发送途中退出仍有候选审计记录。真正的外部投递仍受上面的去重窗口限制。
这些表存有对话材料，应沿用 workspace 的本地数据保护与保留策略。

Drift 的 `message_push` 只暂存草稿，之后动态收窄到 `finish_drift`；同批后续工具也会被执行器拒绝。
没有 finish 的运行记录为 paused，不能投递草稿。它仍沿用 Skill/hazard 设计，不等同于最新 Akashic proposal owner。

## 输入变化与候选修订

判断材料装配时捕获 Session message head 与规则；模型调用前后验证忙闲、head、规则和候选 revision。
过期判断以 superseded 结束 tick，Content 延后重判；Alert 保留未读；不得继续 Drift。
Drift 自身也在执行前后校验用户活动，防止投递已经不合时宜的草稿。

来源内容变化形成新的本地不可变 revision，重置待判断资格。检查状态、消费和 ACK/feedback 结算只针对
当初使用的版本；发送期间到达的新版本不会被旧结果误消费。外部 source ACK 尚无远端 revision CAS，
已开始的渠道发送无法撤回。细节见[ADR0007](../decisions/0007-durable-consumption-and-proactive-supersession.md)。
