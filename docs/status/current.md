# 当前状态

更新日期：2026-10-19。上游参考固定为 Akashic `d85b00c233686b68b082052b1b20123905aeab13`。
实现合同见 [ADR0009](../decisions/0009-components-recovery-and-akasha2.md)，实际运行结果见
[验证记录](../operations/verification.md)。本页只维护能力范围和剩余工作。

## 已落地

- 被动对话、Wake、Drift、子任务和 Scheduler 共用 reasoner 与工具授权边界；渠道、直接和控制入口共用 admission。
- 模型当前生成有界重试、Retry-After、可取消等待、流片段 reset、TUI 状态及截断拒绝；已执行工具不因网络恢复重复。
- Plugin API 3：提供者拥有版本能力，消费者声明依赖并持有 generation lease；资源 scope 物理排空，缺失依赖拒绝发布并撤销贡献。
- 删除 core 的记忆构造工厂与插件 context 的宽泛 session/memory 入口；记忆工厂归 bootstrap，合同归提供者。
- Wake 可选持久兴趣估计、修订身份、质量衰减池与多来源排序；最终 share/skip、版本失效、ACK、投递去重与重试合同保留。
- Drift 持久 proposal、来源 head、skill 摘要、claim/结算和重启终态；仍由显式技能与空闲调度产生提案。
- Akasha v1 保留事务消费与共享重建周期；新增可独立选择的 Akasha2，固定上游算法来源、许可证与文件摘要。
- Akasha2 发布图与消费位置/原文绑定/向量空间的同一快照，默认每 16 条及关闭时 checkpoint；尾部可重放，失败后禁止继续使用脏状态，writer 独占。
- Akasha2 固定向量离线重建工具，不访问网络、不修改来源、不覆盖或自动激活旧库。
- Step Plan 专用路由与 Step5 验证器；真实 reader/judge 评测与确定性合同回归分开记录。

## 保留的基础能力

Session SQLite append-only、上下文投影与 compaction ledger、Shell/PTY/stdin、独立子任务、Scheduler、
插件/MCP 代际、多渠道投递、ACK/feedback、workspace 锁与 Yoyo ledger 继续使用现有实现。
本次没有修改用户真实 workspace、迁移历史记忆或更换已有账户配置。

## 尚未完成

| 工作 | 当前边界 | 下一步验收 |
| --- | --- | --- |
| Akasha2 生产使用 | 新引擎显式 opt-in；固定版本移植不等于全部上游 runtime parity | 完整证据覆盖、显式反馈/管理面、更大历史吞吐与向量空间稳定性 |
| 记忆质量 | 有可复现的固定公开样本四组实验；不等于官方全量榜单 | 全 500 题、多种 embedding、独立 judge、重复运行与区间 |
| v1 大历史性能 | 历史前缀 FTS 重建成本随历史增长 | 分规模测量 p50/p95 后优化索引 |
| 主动体验 | 兴趣池已接入；显式规则与证据估计仍需标注 | 实际来源上的有用率、打扰率、静默正确率及日预算 |
| 自主目标发现 | 提案队列已有持久生命周期，候选仍来自显式技能 | 有证据的 proposal planner、过期与用户意图修正 |
| 组件全面拆分 | 通用能力宿主已接入插件和对话记忆；部分内置工具/维护仍由 bootstrap 装配 | 逐个迁移业务 owner，不能靠目录改名宣称完成 |
| Session/Ledger | 沿用当前 append-only 消息与多份业务状态 | 不宣称已移植上游全部 Ledger 消息程序与跨业务事务 |
| 多租户隔离 | 当前是单用户跨会话记忆；不把 MemoryScope 当租户安全边界 | 显式 isolated/global/off 图路由与独立授权 |
| 未知外部结果 | 持久记录并停止自动重放；投递后崩溃仍可能需要核对 | 工具级幂等键、回执和人工/系统 reconciliation |
| 产品/运维 | 未追平上游移动端、完整 provider catalog、bundle 模式和发布设施 | 根据实际产品需求逐步落地 |

## 迁移边界

`[memory].plugin` 选择 default / akasha / akasha2，三种数据不自动互转。新 Akasha2 首次启动只消费
cutover 后的消息，旧历史需显式重建；恢复要求原始消息仍在且向量空间一致。
旧 v1 图继续可用，但不能由新增消息的事务收据反推全部旧历史具有重放证明。
主动投递保持[去重窗口语义](../decisions/0004-delivery-dedup.md)，不宣称 exactly-once。
