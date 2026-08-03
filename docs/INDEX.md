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
| [记忆评测](./operations/memory-evaluation.md) | 免费确定性 Gate 与模型质量评测边界 |
| [Workspace 迁移](./operations/workspace-migrations.md) | Yoyo、单实例锁和 append-only 规则 |
| [Curated Feeds](./operations/curated-feeds.md) | 内置订阅插件、配置与数据状态 |

## 工程记录

- [工程方法](./engineering/method.md)：如何判断地基问题、验证调用链和清理过渡态。
- [decisions/](./decisions/)：仍需保留的架构决策。被取代的 decision 会明确标为 superseded。

## 维护规则

- 当前事实只写一处，其他文档链接过去。
- 未完成事项只写在 `status/current.md`。
- 实际执行证据只写在 `operations/verification.md`。
- 文件或目录改名时必须运行链接检查。
- `Reference/` 是开发审计输入，不是文档或运行时依赖。
