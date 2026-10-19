# 文档索引

这里是项目文档的唯一入口。文档按用途分类，不再区分仓库根目录和 `_handbook`。

## 建议阅读顺序

1. [架构总览](./architecture/overview.md)：先了解运行链、目录 owner 和数据边界。
2. [当前状态](./status/current.md)：确认已完成、明确延期和仍可加厚的部分。
3. 按任务进入 handbook 或 architecture。
4. 需要判断“是否真的跑过”时看 [验证记录](./operations/verification.md)。

## 当前状态

| 文档 | 内容 |
| --- | --- |
| [当前状态](./status/current.md) | 已完成能力、明确延期、非阻塞增强项 |
| [Reference 对齐状态](./status/reference-alignment.md) | 对齐口径、已闭合 gap、仍保留的差异 |

## 使用手册

| 文档 | 适合什么时候读 |
| --- | --- |
| [Session、CLI 与 TUI](./handbook/sessions-and-cli.md) | 启动本地客户端、切换 Session、理解流式终态 |
| [上下文治理](./handbook/context.md) | 排查上下文过长、历史投影、压缩和 token usage |
| [工具、Shell、子 Agent 与 Scheduler](./handbook/tools-and-scheduler.md) | 使用执行工具、后台任务、子 Agent 和定时任务 |
| [使用记忆系统](./handbook/memory.md) | 配置 Default/Akasha、embedding、管理与排错 |
| [开发和管理插件](./handbook/plugins.md) | 编写插件、安装、启停和查看错误 |
| [配置 Workspace MCP](./handbook/workspace-mcp.md) | 声明、热更新和排查 MCP server |
| [使用主动推送](./handbook/proactive.md) | 配置 Proactive、数据源、ACK 和轨迹 |
| [使用 Drift](./handbook/drift.md) | 编写后台技能、理解收尾和连续性 |

## 架构设计

| 文档 | 主题 |
| --- | --- |
| [架构总览](./architecture/overview.md) | 三条运行链、owner、依赖方向与持久状态 |
| [启动与渠道](./architecture/startup-and-channels.md) | supervisor、gateway、workspace、Channel 和投递 |
| [控制面](./architecture/control-plane.md) | JSON-RPC、turn 状态机、事件流和重启协调 |
| [记忆架构](./architecture/memory.md) | MemoryServices、Default、Akasha 和真相源 |
| [插件架构](./architecture/plugins.md) | 声明、代际、准入、热重载和资源回收 |
| [Proactive 与 Drift](./architecture/proactive.md) | 模块 DAG、提交边界、feedback 和运行轨迹 |
| [快照与租约](./architecture/snapshot-leases.md) | 在途 turn 如何锁定能力代际 |

## 运行与验证

| 文档 | 内容 |
| --- | --- |
| [验证记录](./operations/verification.md) | 当前离线、在线和 Reference 独立性结果 |
| [记忆评测](./operations/memory-evaluation.md) | 离线合同、Step5 实测和固定质量协议 |
| [组件收敛与实测](./operations/evaluations/2026-10-19-ownership.md) | 提供者归属、失败回收与 Step5 记忆工具链 |
| [2026-10-19 实验结果](./operations/evaluations/2026-10-19-memory.md) | 两轮四组与 oracle 的逐题结果和限制 |
| [Workspace 迁移](./operations/workspace-migrations.md) | Yoyo、单实例锁和 append-only 规则 |
| [Curated Feeds](./operations/curated-feeds.md) | 内置订阅插件、配置与数据状态 |

## 工程记录

- [组件、恢复与 Akasha2](./decisions/0009-components-recovery-and-akasha2.md)：2026-10-19 的实现合同、来源与验证边界。

- [Akashic Vibe Coding 工作流](./engineering/akashic-vibe-coding-workflow.md)：截至 2026-10-07 的完整工作流与可复制任务说明。
- [Akashic 工作流修订](./engineering/akashic-workflow-2026-10-03.md)：当前三步流程、旧稿勘误、源码证据和 Kirakira 的轻量实践。
- [共享执行器与运行日志](./decisions/0008-core-execution-and-admission.md)：入口统一、排队取消、工具不确定结果与恢复边界。
- [2026-10-02 对照审计](./engineering/akashic-review-2026-10-02.md)：双主动、Akasha2 取舍、Akashic/Maka/DeepSeek Harness 比较与本轮重构。
- [持久补消费与主动过期判断](./decisions/0007-durable-consumption-and-proactive-supersession.md)：第二轮对齐、源身份、候选版本与失败恢复。
- [共享推理与提交后学习](./decisions/0006-shared-reasoning-and-committed-memory.md)：本轮运行合同和恢复边界。
- [工程方法](./engineering/method.md)：如何判断地基问题、验证调用链和清理过渡态。
- [decisions/](./decisions/)：仍需保留的架构决策。被取代的 decision 会明确标为 superseded。

## 维护规则

- 当前事实只写一处，其他文档链接过去。
- 未完成事项只写在 `status/current.md`。
- 实际执行证据只写在 `operations/verification.md`。
- 文件或目录改名时必须运行链接检查。
- `Reference/` 是开发审计输入，不是文档或运行时依赖。
