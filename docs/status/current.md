# 当前状态

更新日期：2026-10-20。主链参考核对至 Akashic `6016befc682a037fbfc61180d39f17c219572ed6`。
记忆移植继续固定 `d85b00c233686b68b082052b1b20123905aeab13`；算法、双通道与公开 cost 协议未变。
实现合同见 [ADR0009](../decisions/0009-components-recovery-and-akasha2.md) 和
[ADR0015](../decisions/0015-projects-and-session-directories.md)，实际运行结果见
[验证记录](../operations/verification.md)。本页维护阶段和能力范围，逐项验收来自
[完整功能清单](akashic-features.md)及其 JSON 数据源。

项目统一使用 Python 3.14.6：`.python-version` 固定解释器，`uv.lock` 固定依赖，
本地使用项目 `.venv`，CI 读取相同版本文件并只执行一个任务；不再维护多版本测试矩阵。

## 当前骨架

Context/Fiber/Effect、OwnerCall、事件、健康与拓扑使用固定提交的原生代码。
`agent/plugin_composition/UPSTREAM.json` 和 CI 锁定逐文件摘要；没有并行的自建依赖图。
普通业务 provider 直接使用原生 Context，默认组合位于 `bootstrap/distribution.py`。
`agent/core` 与顶层 `session` 已移除，主链按 Ledger / Sources / Conversation /
Reply Program / Context / ReAct / Models / Tools / MCP / Delivery 分为普通 owner。
类式插件只在扩展加载边界适配，已有功能在同一原生 Root 中接回。

架构合同以 [Runtime 架构](../architecture/overview.md) 和
[所有权与局部更新](../architecture/ownership-and-reload.md) 为准。上游完整功能仍需按清单补齐，
不把原生内核一致性当成功能全部完成。

## 已落地

- 独立 Jobs owner：后台结果持久发布、逐模型步领取、空闲原会话唤醒；消费与最终消息/程序同事务，取消与重启可回放。
- 子代理不再直接排入合成用户消息，结果按原会话查询；移除重复的 `subagent-runs/result.json` 写入。
- 当前主链、Jobs、Scheduler、项目/目录、Wake/Drift 最终 Step5 场景 64/64；中间失败及量化结果见[报告](../operations/evaluations/2026-10-20-background-results.md)。
- Projects 与 Session cwd 接入：稳定项目 ID、改名/归档、一次性目录默认值、创建事务快照、独立切换与回执/CAS。
- 文件、Shell、子任务与实时 AGENTS 共用目录 owner；独占切换批次、现有进程 cwd 固定、旧会话不回填。
- Web 项目/目录页面、项目聊天和控制协议共用同一服务及 admission；目录丢失不回退或自动重建。
- 显式 Session 删除与目录/回执同事务清理；删除失败回滚，重新创建路由重新继承默认值。
- 被动对话、Wake、Drift、子任务和 Scheduler 共用 reasoner 与工具授权边界；渠道、直接和控制入口共用 admission。
- Scheduler 提供者使用来源会话、notify/run、none/session 历史与持久 fire；投递回执后完成，未知结果停止自动重放。
- 删除未接线的旧同步 Agent、手工压缩入口和图快照代码；渠道排队与中断统一归 admission。
- 模型当前生成有界重试、Retry-After、可取消等待、流片段 reset、TUI 状态及截断拒绝；已执行工具不因网络恢复重复。
- 显式安全读取使用有界并发，写入/加载/终结工具保持屏障；子调用保存执行事实并使用具体工具菜单，结果按模型顺序入上下文。
- 流结束使用完成通知；重复取消保留模型清理和同步工具线程的物理排空；关闭覆盖直接与排队入口。
- 中断控制状态在下一请求有明确 system 提示，重启后仍有效；日志未结算检查避开大模型正文解码。
- Plugin API 3：提供者拥有版本能力，消费者声明依赖，实际调用保留确切 Fiber activation；资源 scope 物理排空，缺失依赖拒绝发布并撤销贡献。
- 记忆构造归 default_memory provider、工具归 memory_tools 提供者；工作区工具不持有记忆服务，Drift 显式只读记忆。
- 插件公共 API 与 manager 分离，删除无效服务包装与未使用的宿主参数；CLI 与渠道/主动装配分离。
- 装配/启动失败回收、后台任务所有权、取消等待物理关闭、失败后继续关闭和错误聚合已接入。
- Wake 可选持久兴趣估计、修订身份、质量衰减池与多来源排序；最终 share/skip、版本失效、ACK、投递去重与重试合同保留。
- Drift 持久 proposal、来源 head、skill 摘要、claim/结算和重启终态；仍由显式技能与空闲调度产生提案。
- Akasha v1 保留事务消费与共享重建周期；新增可独立选择的 Akasha2，固定上游算法来源、许可证与文件摘要。
- Akasha2 的宿主读出补齐精确向量与联想双通道：去重、过滤、各通道时间排序与总记录预算；算法文件保持原样。
- 召回 JSON 消息范围可直接展开原文；跨命中的校验共用只读快照，恢复源库连接固定为两次。
- Akasha2 新学习输入按原文与完整空间身份保存 float32 固定向量，学习失败复用、源库独立重建；旧历史不自动回填。
- Akasha2 发布图与消费位置/原文绑定/向量空间的同一快照，默认每 16 条及关闭时 checkpoint；尾部可重放，失败后禁止继续使用脏状态，writer 独占。
- Akasha2 固定向量离线重建工具，不访问网络、不修改来源、不覆盖或自动激活旧库。
- Step Plan 专用路由与 Step5 验证器；真实 reader/judge 评测与确定性合同回归分开记录。

## 保留的基础能力

Session SQLite append-only、上下文投影与 compaction ledger、Shell/PTY/stdin、独立子任务、Scheduler、
插件/MCP 的局部激活、多渠道投递、ACK/feedback、workspace 锁与 Yoyo ledger 继续使用现有实现。
本次没有修改用户真实 workspace、迁移历史记忆或更换已有账户配置。

## 功能推进顺序

范围包含上游全部 70 个生产插件（含可选提供方），以及 SDK、部署与客户端，不按当前默认启用集缩小。
清单中的 partial/missing 都属于未完成，不能用测试数量、同名工具或算法来源 pin 充当整体对齐证据。

| 顺序 | 功能链 | 完成要求 |
| --- | --- | --- |
| 1 | 消息、来源与回复程序 | 在已接入的后台结果步间注入之上，补统一持久 Message/附件、steer 暂存/撤回、latest 输入重试及完整程序合同 |
| 2 | 项目与执行宿主 | 在已接入的本地 Project/cwd 之上，补 prepared 目标、HostBridge、进程/Workload 生命周期 |
| 3 | 双主动与投递 | Eventmail、Wake/Drift 完整状态链，Scheduler 工具边界让出，按固定来源投递和核对 |
| 4 | 扩展与模型 | 插件发布/更新回执、资源提供方、UI 与检查能力；模型目录、角色、启停、引导及全部列出的驱动 |
| 5 | 用户工作台 | 终端、改动视图、标题、附件/visuals、sites、浏览器、认证与完整会话流程 |
| 6 | SDK 与交付 | SDK、Web/Android 协议、安装组合、部署、备份恢复和运维入口 |

记忆保留当前实现与证据，继续列在全量范围内；本阶段优先补非记忆功能。
整体吞吐、事件循环 lag 和记忆吞吐优化暂缓，历史性能结果保留，不作为功能验收替代品。

本轮重构后复测发现本地主链吞吐回退：同机千轮中位数 87.19→54.15 轮/s（−37.90%）。
已接入功能的正确性合同通过，性能尚未恢复；见[性能复核](../operations/evaluations/2026-10-20-runtime-parity.md)。

## 尚未完成的合同

| 工作 | 当前边界 | 下一步验收 |
| --- | --- | --- |
| Akasha2 生产使用 | 新引擎显式 opt-in；固定版本移植不等于全部上游 runtime parity | 完整证据覆盖、显式反馈/管理面、更大历史吞吐与向量空间稳定性 |
| 记忆质量 | 有 36 题重复、24 题保留集及另 12 题读出验证；包含检索消融与真实 Mem0 子集，不等于官方全量榜单 | 全 500 题、多种 embedding、独立 judge、重复运行与区间 |
| 主动体验 | 兴趣池已接入；显式规则与证据估计仍需标注 | 实际来源上的有用率、打扰率、静默正确率及日预算 |
| Scheduler 续跑 | 来源会话、notify/run、历史隔离、触发结算已接入；仍是整轮排队 | 工具边界让出用户、保存并恢复同一持久程序；已完成副作用不重放 |
| 自主目标发现 | 提案队列已有持久生命周期，候选仍来自显式技能 | 有证据的 proposal planner、过期与用户意图修正 |
| 扩展功能补齐 | 原生内核、普通业务 owner 与 Jobs 已接入；旧类式 SDK 集中适配 | 原生 Task 服务、Shell 等全部后台 producer、材料协议及完整执行宿主按功能清单继续补齐 |
| 项目与目录 | 本地端到端功能已接入，不等于整个上游执行宿主或工作台 | prepared 目标与恢复、宿主路径能力、终端和改动视图 |
| Session/Ledger | 沿用当前 append-only 消息与多份业务状态 | 不宣称已移植上游全部 Ledger 消息程序与跨业务事务 |
| 多租户隔离 | 当前是单用户跨会话记忆；不把 MemoryScope 当租户安全边界 | 显式 isolated/global/off 图路由与独立授权 |
| 未知外部结果 | 持久记录并停止自动重放；投递后崩溃仍可能需要核对 | 工具级幂等键、回执和人工/系统 reconciliation |
| 产品/运维 | 未追平上游移动端、完整 provider catalog、bundle 模式和发布设施 | 按完整功能清单逐项落地实际入口与验证 |

## 迁移边界

`[memory].plugin` 选择 default / akasha / akasha2，三种数据不自动互转。新 Akasha2 首次启动只消费
cutover 后的消息，旧历史需显式重建；恢复要求原始消息仍在且向量空间一致。
旧 v1 图继续可用，但不能由新增消息的事务收据反推全部旧历史具有重放证明。
主动投递保持[去重窗口语义](../decisions/0004-delivery-dedup.md)，不宣称 exactly-once。
