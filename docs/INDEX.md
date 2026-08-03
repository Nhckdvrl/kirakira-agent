# 文档索引

文档只保留三类：当前合同、架构/决策依据、真实验证记录。过时的阶段流水账、重复路线图和对外
包装稿已删除；未完成事项只写在 `NOW.md`。

## 从这里开始

| 目的 | 文档 |
| --- | --- |
| 当前整体能力与运行命令 | [README-cn.md](../README-cn.md) |
| 当前延期项 | [NOW.md](./NOW.md) |
| 与最新版 Reference 的完整能力审计 | [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md) |
| 确认哪些是离线测试、哪些实际跑过 | [design/live-verification.md](./design/live-verification.md) |
| 工程判断方法 | [ENGINEERING_METHOD.md](./ENGINEERING_METHOD.md) |

## 当前专题

| 子系统 | 文档 |
| --- | --- |
| 上下文投影、预算、压缩与 usage | [_handbook/context-management.md](../_handbook/context-management.md) |
| Session/TUI | [_handbook/cli-and-sessions.md](../_handbook/cli-and-sessions.md) |
| 记忆与 Akasha | [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md)、[_handbook/memory.md](../_handbook/memory.md) |
| 记忆评测 | [MEMORY_EVALUATION.md](./MEMORY_EVALUATION.md) |
| 插件与 MCP | [PLUGIN_SYSTEM.md](./PLUGIN_SYSTEM.md)、[_handbook/plugins.md](../_handbook/plugins.md)、[_handbook/workspace-mcp.md](../_handbook/workspace-mcp.md) |
| Snapshot/lease | [_handbook/snapshot-and-lease.md](../_handbook/snapshot-and-lease.md) |
| Proactive/Drift/轨迹 | [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md)、[_handbook/proactive.md](../_handbook/proactive.md)、[_handbook/drift.md](../_handbook/drift.md) |
| 启动、Supervisor、渠道 | [STARTUP_AND_CHANNELS.md](./STARTUP_AND_CHANNELS.md) |
| 控制面 | [design/control-plane.md](./design/control-plane.md) |
| 日本 AI/机器人 curated feed | [JP_AI_ROBOTICS_FEEDS.md](./JP_AI_ROBOTICS_FEEDS.md) |

## 架构与决策依据

- [design/proactive-lifecycle.md](./design/proactive-lifecycle.md)：主动模块流水线与快照租约。
- [decisions/0001-plugin-slot-ordering-opt-in.md](./decisions/0001-plugin-slot-ordering-opt-in.md)：插件 slot 排序。
- [decisions/0002-consolidation-driver.md](./decisions/0002-consolidation-driver.md)：历史决策，已被 0003 取代。
- [decisions/0003-consolidation-handover.md](./decisions/0003-consolidation-handover.md)：Markdown maintenance 接管 consolidation。
- [decisions/0004-delivery-dedup.md](./decisions/0004-delivery-dedup.md)：投递去重边界。
- [decisions/0005-drift-hazard-sampled-expiry.md](./decisions/0005-drift-hazard-sampled-expiry.md)：Drift 采样到期。

Decision 是历史依据，不因实现更新而改写；被推翻时新增记录并互相引用。Design 中的旧故障描述
只作为当时证据，当前状态以 README、handbook、NOW 与 DIFFERENCE_AUDIT 为准。
