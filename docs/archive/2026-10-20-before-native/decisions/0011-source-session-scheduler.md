# 0011：来源会话、持久触发与回执结算

日期：2026-10-19。状态：已实现下述合同。

Scheduler 是 `plugins/scheduler` 提供者，bootstrap 注入 Pipeline、SessionManager、执行日志与总线。
Core 只提供 admission、来源元数据、历史投影和提交消息 ID，不理解定时任务业务。
这遵循 Akashic 固定提交 `d85b00c` 的
[ADR0125](https://github.com/kachofugetsu09/akashic-agent/blob/d85b00c233686b68b082052b1b20123905aeab13/docs/decisions/0125-scheduler-runs-in-its-session.md)。

## 执行与事实

任务绑定创建它的 session/channel/chat；模型不能覆盖投递目标。notify 原样提交并投递，run 使用
同一会话 Pipeline。none 历史不加载聊天或触发整段聊天压缩；session 历史显式包含会话。
渠道入口删去重复锁，全部对话入口共用 TurnAdmission 的排队与中断。

任务快照与 fire 同文件原子发布。fire 身份由 job ID 和 UTC 触发时间确定；开始模型前记录 running，
投递前记录 delivering 及真实 assistant ID，回调成功后 completed。通知原文、run 输入和回复留在
创建会话，来源为 scheduler:fire-key。列表和取消都按创建会话约束。

只有 conversation、programmatic、无归属的旧消息可以参与记忆；显式 skip/proactive 仍排除。
该边界用于原文消费、Markdown 归档、Wake presence/近期上下文与 Drift 空闲基线。后台任务不能
通过写记忆工具绕过排除。只含排除来源的归档窗口推进游标，不调用模型、不产生用户档案事件。

## 失败、取消与恢复

尚未 admission 的 pending fire 可恢复。running/delivering 遗留状态与不明确的投递结果进入
needs_reconciliation；不自动重放未知副作用或重发。取消等待实际工具 finally 收束，之后根据
执行日志判断已知取消或待核对。保存失败会使当前 store 不可继续使用，重新打开确认磁盘事实。

Yoyo 新增独立迁移，验证整个旧任务快照后保存精确字节备份，再发布 v2。旧初始化向导的空列表
也有明确迁移；损坏与未知字段拒绝处理。既有迁移文件不修改，用户真实数据库不在测试中打开。
仓库停止跟踪根 sessions.db，已有本地文件保留并忽略。

## 边界

没有移植上游完整 Ledger 程序模型，尚不支持工具边界让出后恢复同一个悬挂程序。当前采用整轮
排队和保守终止。原始会话、执行日志、任务文件不是跨文件事务；崩溃窗口保留待核对状态。
成功是本地投递回调返回，不是外部平台 exactly-once 或用户已读。快照保留 fire 历史，尚无保留策略。

验证见[本轮记录](../operations/evaluations/2026-10-19-scheduler.md)。
