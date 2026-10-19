# 0007：持久补消费与主动决策过期

日期：2026-10-02。状态：accepted。承接 [0006](0006-shared-reasoning-and-committed-memory.md)。

最新审计点为 Akashic `7cf10136932d5fe0205727c803c00861272db0b8`。本轮继续对齐
“消费来源与图一起前进”“读取集合变化后不再发布旧结果”的原则，不移植整套 v2/Message runtime。
上游参考：[MessageConsumer](https://github.com/kachofugetsu09/akashic-agent/blob/7cf10136932d5fe0205727c803c00861272db0b8/plugins/akasha/application/consumer.py)、
[React](https://github.com/kachofugetsu09/akashic-agent/blob/7cf10136932d5fe0205727c803c00861272db0b8/plugins/react/plugin.py)。

## 原始消息与补消费

SessionDB 新增 `message_publications`，通过 trigger 在 assistant 消息写入的同一事务分配单调递增位置。
这里只保存 message ID 与 session key，不复制消息内容。独立 stream ID 表示该原始数据库的消费身份。
删除消息不会重用通知位置，重建/恢复时不能把另一个源库接到旧消费游标上。

Akasha 首次激活记录当前 head 作为 cutover，不自动重学激活前历史。之后通知只负责唤醒，学习必须从
SessionDB 的持久序列读取。每批最多 64 条，失败停在原位置，后续通知不能绕过失败条目。
成功的图变更、收据和 source cursor 同事务提交；不合格/已删除来源写明确 skip 收据再推进。
固定向量生成失败或取消不推进游标，已缓存的固定向量可被重试复用。

Runtime 通过 MemoryServices 启动引擎维护任务：启动即扫描，此后每 30 秒扫描，并由 runtime 在关闭存储前
取消并等待任务。普通 TurnCommitted 同样触发扫描；查询保持不学习。源流身份改变或源进度回退明确失败，
不会静默“从零再学”。若恢复备份，应恢复配套 source/sidecar，或显式重建。

重建候选库记录源快照的 stream ID 与 head，因此快照之后、切换之前到达的消息仍可继续消费。
重建仍不调用 provider；启用后的在线补消费会按既有 embedding 配置为新消息生成缺失向量。

这是增量 cutover：迁移不补写旧消息的 publication。启用前的历史与升级前遗漏的通知仍需显式重建。
v1 的学习时间顺序约束仍保留，倒序/冲突来源不会被自动排序掩盖。单批上限使积压恢复分批进行。

## 主动输入版本

Judge context 建立时捕获目标 Session 的 durable message head 和主动规则文本。
内容和告警在调用模型前、模型返回后检查：目标是否进入被动处理、会话 head 是否变化、规则是否变化、
候选是否仍为未读的同一 revision。检查失败抛出 `DecisionSuperseded`，模块以 superseded 终止本 tick，
不得继续 Drift。Content 保留候选并延后 300 秒重判；stale skip 同样不能永久封掉新候选。

Drift 在启动时检查忙闲并捕获会话 head，结束时再次检查；过期草稿留在内部，运行记录为 paused。
这保护对外消息，不承诺撤回 Drift 先前已经执行的工具副作用。

## 候选修订与结算

Proactive state 是本地事件 owner：同一 item ID 的来源内容变化生成递增 revision，旧 payload 保存在
`event_revisions`，当前 unread 投影更新且原检查资格重置。完全相同的抓取不生成新版本。
同一 item ID 不能悄悄改变 source owner 或 channel。

判断只针对冻结版本。消费、检查状态和 Alert ACK 排队都验证当前版本；Content feedback 也只为成功结算
的版本排队。即使 source 在渠道发送期间更新，旧发送结果也不能消费或反馈新版本。

外部 source ACK 协议仍只支持 event ID，没有远端 revision CAS；本轮保证的是本地版本结算。
检查之后已经交给外部渠道的发送无法撤回，投递继续遵循 [0004](0004-delivery-dedup.md) 的去重窗口语义。
这不是 exactly-once，也不是完整上游 Eventmail/质量衰减池。

## 验证

`test_memory_recovery.py` 验证漏通知、失败顺序、首次 cutover、取消、写入回滚、源事务、维护任务、
源库身份和重建后续接。`test_proactive_admission.py` 验证版本升级/重启、模型等待期间四类输入变化、
渠道等待期间的修订结算；`test_drift.py` 验证用户活动使草稿失效。
全量执行证据见[验证记录](../operations/verification.md)。
