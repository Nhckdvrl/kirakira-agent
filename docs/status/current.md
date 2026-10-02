# 当前状态

更新日期：2026-10-02。本轮改动基于本地 `b6c4143`，上游 pin 与分析见[重构审计](../engineering/akashic-review-2026-10-02.md)。

## 第二轮对齐

上游审计点更新为 `7cf10136932d5fe0205727c803c00861272db0b8`；契约见
[ADR0007](../decisions/0007-durable-consumption-and-proactive-supersession.md)。

- Session 消息与 publication 位置原子提交，Akasha 从持久位置按序补消费；失败不越过、重启不重学。
- Runtime 管理每 30 秒运行的记忆维护任务；首次激活固定 cutover，源身份/回退不一致明确失败。
- 重建候选保留快照 head，切换后继续处理快照之后的消息。
- Content/Alert 的判断检查会话 head、忙闲、规则和候选版本；旧决策终止 tick，不继续 Drift。
- 本地事件保留不可变修订；旧判断和发送结果不能消费、检查或反馈新版本。
- Drift 用户活动变化后不投递旧草稿；仍保留既有 Skill/hazard 产品语义。

## 第一轮完成

- 被动对话、主动决策和 Drift 共用 `DefaultReasoner`；后台入口显式限制工具集合、终结动作和步数。
- 主动内容不再解析回复中的 JSON：使用 share/skip 工具、冻结候选 ID 校验和冲突批次拒绝。
- 候选检查、失败重试和决策输入/轨迹持久化；冷却期间到达的事件可在重启后继续判断；tick 串行化。
- Drift 使用异步共享执行器；草稿产生后工具授权收窄，必须 finish 才能提交草稿。
- Akasha v1 查询不学习；完整持久消息对在提交后学习。使用稳定消息 ID，图更新与幂等消费收据同事务。
- 线上学习和离线重建共用 cycle；上一轮激活可重启恢复，学习的 dense/FTS/IDF 只见已学习历史前缀。
- 新增只读快照重建命令：验证固定向量与配置，发布新 sidecar，不覆盖或自动激活旧库。
- 删除退休的查询学习/缓存更新旁路，扩展变更影响门禁；修复 CLI 参数验证顺序及测试 workspace 隔离。

## 既有能力

Session SQLite append-only、上下文投影/compaction ledger、Shell/PTY/stdin、独立子任务、Scheduler、
插件代际和 snapshot lease、MCP、多渠道投递、ACK/feedback、去重窗口、workspace 锁与 Yoyo ledger 均保留。
本轮没有重跑收费在线验证；历史在线结果与本次离线结果分开记录在[验证记录](../operations/verification.md)。

## 尚未完成与下一阶段

| 工作 | 当前边界 | 进入条件/验收方向 |
| --- | --- | --- |
| Akasha2 算法迁移 | 仍为 v1；事务/重放合同不是 v2 parity | 同 embedding/语料/预算的 dense、v1、v2 消融后决定 |
| 记忆恢复扩展 | 新 cutover 后支持持久补消费；升级前缺口不自动补学 | 显式重建处理旧历史；进一步测量大积压吞吐 |
| 大历史性能 | 学习时重建历史前缀内存 FTS，成本随历史增长 | 测量 p50/p95、历史规模与内存，再做增量索引 |
| 主动质量池 | 有持久 checked/retry；有本地版本/CAS，仍没有上游 decaying eventmail pool | 来源修订/CAS、到达驱动、日预算、价值/打扰标注实验 |
| 内部主动提案 | 仍是 Skill + hazard + scratchpad | 明确 proposal 生命周期、选择和投递结算后替换 |
| 持久程序执行 | scoped 调用没有上游完整消息日志、writer grants、supersession 和续跑 | 模型/工具中断后恢复语义明确，不重复外部副作用 |
| 跨用户/隔离图 | 传递真实 MemoryScope；不等于 global/isolated/off 图路由 | 明确单用户跨渠道与多租户的不同合同 |
| 完整抢占/外部副作用 | 模型返回后和 Drift 提交前已检查活动与来源；已交给渠道的发送无法撤回 | 未来持久程序和外部工具的取消/恢复合同 |
| 真实质量评测 | 离线回归不是 LongMemEval/PersonaMem 或主动体验质量结果 | 固定数据、预算、judge、原始轨迹、失败与重复尝试策略 |
| 产品/运维扩展 | 未追平上游移动端、完整 provider catalog 与发布设施 | 由实际产品需求驱动，不为目录对齐扩张 |

## 已知迁移边界

旧 v1 图没有消费收据，可继续摄入新 turn；重试旧 turn 或改变算法参数/embedding 空间需要显式重建。
新建数据库测试中的 online/replay 一致，不能反推未经重建的旧库也具有完整来源证明。
主动投递仍为[去重窗口语义](../decisions/0004-delivery-dedup.md)，不是 exactly-once。
