# Reference 对齐状态

参考日期：2026-10-19。主链检查至 Akashic
[`21b38f0e102d9e1bdc5576725635dce29f135c0c`](https://github.com/kachofugetsu09/akashic-agent/tree/21b38f0e102d9e1bdc5576725635dce29f135c0c)，记忆算法来源继续固定
[`d85b00c233686b68b082052b1b20123905aeab13`](https://github.com/kachofugetsu09/akashic-agent/tree/d85b00c233686b68b082052b1b20123905aeab13)。
Kirakira 按职责、运行合同和证据对齐，不按目录数量或功能名称判断整体 parity。

| 领域 | Kirakira 当前落点 |
| --- | --- |
| Core | 共享 reasoner、显式安全读取并发、授权、当前生成恢复、物理排空与中断控制提示；执行日志保留真实完成顺序，没有移植上游逐 Message 的提交事务 |
| Composition | 有版本能力、显式依赖、代际借用与资源 scope；工具提供者与插件 API 已拆开，失败回收统一；部分内置业务仍由组合根装配 |
| Wake | 持久兴趣评分、衰减池、多来源候选、share/skip 工具及修订/CAS 结算；不等同完整 Eventmail |
| Drift | 技能、空闲采样、连续性及持久 proposal 状态；不是完整自主目标发现系统 |
| Akasha2 | 固定上游 MemoryCycle/domain/persistence，自有消费 adapter、快照与宿主 dense/completion 双通道；总记录预算，显式 opt-in |
| Memory evidence | 原始消息回源、提交后学习、公开机制复测、72 个问题与真实 Mem0 子集；不继承上游质量结论 |
| Session/Context | append-only 消息、compaction ledger、预算投影；未全面迁移最新上游 Ledger |
| Scheduler | 来源会话、notify/run、none/session 历史、持久 fire 与回执结算；排队共享 admission，尚无工具边界让出并恢复同一程序 |
| 产品面 | TUI/Web、渠道、MCP、Scheduler 保留；不宣称移动端或完整部署能力对齐 |

来源、实现与合同见 [ADR0009](../decisions/0009-components-recovery-and-akasha2.md)。
未完成事项只在[当前状态](./current.md)维护，实际结果只在[验证记录](../operations/verification.md)维护。

## 独立分发

生产包包含移植的算法代码、MIT 许可证与 `UPSTREAM.json` 摘要；不读取或导入 Reference checkout。
测试、构建与 CLI 均应能在没有上游目录的环境运行。固定 commit 是来源，不是运行依赖。
