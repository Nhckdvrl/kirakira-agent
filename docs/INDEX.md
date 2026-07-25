# 文档索引

本文件只维护路径、阅读顺序和选择条件,不复制正文。新增、移动、改名或废弃文档时同步更新本索引。
写作规范沿用 `Reference/docs/writing-rules.md`。

## 四类文档,各有各的职责

| 类别 | 回答什么 | 位置 |
| --- | --- | --- |
| **现状** | 现在是什么形态、还差什么 | `MVP_TO_CURRENT.md`、`NOW.md`、各子系统专题 |
| **历程** | 为什么长成这样、每一步解决了什么问题 | `VERSION_EVOLUTION.md` |
| **判断** | 与 Reference 差在哪、哪些是有意取舍 | `DIFFERENCE_AUDIT.md` |
| **依据** | 某个选择的理由、某次重构的调用链与验收 | `decisions/`、`design/` |

一个事实只设一个权威位置,其余文档用链接引用。未完成事项只写在 `NOW.md`。

## 按任务选择

| 你要做的事 | 读哪份 |
| --- | --- |
| 接手未完成的工作、找当前阻塞 | [NOW.md](./NOW.md) |
| 了解项目从 MVP 长到现在的整体形态 | [MVP_TO_CURRENT.md](./MVP_TO_CURRENT.md) |
| 了解被动链路每一轮工程化解决了什么问题 | [VERSION_EVOLUTION.md](./VERSION_EVOLUTION.md) |
| 查与 Reference(Akashic)的差距与定位 | [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md) |
| **确认某条链路是"测过"还是"真跑过"** | [design/live-verification.md](./design/live-verification.md) |
| 改记忆:引擎、检索、摄入、embedding | [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md) |
| 改插件:声明、代际、热重载、安装 | [PLUGIN_SYSTEM.md](./PLUGIN_SYSTEM.md) |
| 改主动推送或 Drift | [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md) + [design/proactive-lifecycle.md](./design/proactive-lifecycle.md) |
| 改启动、Supervisor 或渠道 | [STARTUP_AND_CHANNELS.md](./STARTUP_AND_CHANNELS.md) |
| 程序化观测/驱动运行中的 agent | [design/control-plane.md](./design/control-plane.md) |
| 查某个架构选择当初为什么这么定 | [decisions/](./decisions/) |
| 准备面试/对外讲这个项目 | [RESUME_INTERVIEW_GUIDE.md](./RESUME_INTERVIEW_GUIDE.md) |

## 阅读顺序(新接手)

1. [MVP_TO_CURRENT.md](./MVP_TO_CURRENT.md) —— 当前整体形态与四个地基的状态。
2. [design/live-verification.md](./design/live-verification.md) —— 哪些是真跑过的,哪些只是测过。
3. [NOW.md](./NOW.md) —— 还没做完的是什么、接手点与验收边界在哪。
4. 按你要动的子系统读对应专题文档。
5. 动手前查 [decisions/](./decisions/) 与 [design/](./design/) 有没有相关约束。

## 决策记录

按编号只增不改;被推翻时新建记录并双向标注,不删原文。

| 编号 | 主题 | 状态 |
| --- | --- | --- |
| [0001](./decisions/0001-plugin-slot-ordering-opt-in.md) | 相位 slot 排序为何在全员声明后才启用 | accepted |
| [0002](./decisions/0002-consolidation-driver.md) | consolidation 暂由 MemoryRuntime 驱动 | superseded by 0003 |
| [0003](./decisions/0003-consolidation-handover.md) | 驱动权移交 MarkdownMemoryMaintenance(含两次勘误) | accepted |
| [0004](./decisions/0004-delivery-dedup.md) | 用内容指纹+时间窗做跨崩溃去重,不做两阶段 outbox | accepted |
| [0005](./decisions/0005-drift-hazard-sampled-expiry.md) | Drift 用采样到期驱动,不用轮询判阈 | accepted |

## 技术设计

| 文档 | 主题 |
| --- | --- |
| [proactive-lifecycle.md](./design/proactive-lifecycle.md) | 主动链路从扁平 tick 重构成模块流水线 |
| [control-plane.md](./design/control-plane.md) | 控制面分层、turn 状态机、事件流与认证 |
| [live-verification.md](./design/live-verification.md) | 实弹验证记录与仍未验证的边界 |

## 历史记录

| 文档 | 状态 |
| --- | --- |
| [MEMORY2_M0_M1.md](./MEMORY2_M0_M1.md) | 历史。记忆 M0/M1 的迁移与恢复过程;当前权威是 [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md),命名已 memory2→coremem |

## 与 Reference 文档体系的差异

Reference 的 `docs/` 还有 `WORKFLOW.md`、`projectneed.md`、`templates/`。kirakira 目前是单人
学习型项目,没有多人交付流程与稳定需求条款的实际需要,因此未建立;等出现真实协作需求时再补,
不先摆空文件。
