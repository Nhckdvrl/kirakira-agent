# 项目文档

先读[架构总览](architecture/overview.md)，再看[当前状态](status/current.md)和
[完整功能清单](status/akashic-features.md)。运行证据统一从[验证入口](operations/verification.md)进入。

## 使用

- [启动、Session、CLI 与 TUI](handbook/sessions-and-cli.md)
- [项目与工作目录](handbook/projects-and-directories.md)
- [上下文治理](handbook/context.md)
- [工具、Shell、子任务与 Scheduler](handbook/tools-and-scheduler.md)
- [记忆配置与管理](handbook/memory.md)
- [插件开发与管理](handbook/plugins.md)、[Workspace MCP](handbook/workspace-mcp.md)
- [主动交互](handbook/proactive.md)、[Drift](handbook/drift.md)

## 架构

- [Runtime 与业务 owner](architecture/overview.md)
- [所有权与局部更新](architecture/ownership-and-reload.md)
- [启动与渠道](architecture/startup-and-channels.md)、[控制面](architecture/control-plane.md)
- [插件](architecture/plugins.md)、[记忆](architecture/memory.md)、[主动运行](architecture/proactive.md)
- [设计决策](decisions/)：按当时版本解释取舍；当前装配和生命周期以以上架构页为准。

## 验证与运维

- [当前验证结果与历史入口](operations/verification.md)
- [后台结果与最新 main 的 Step5 验证](operations/evaluations/2026-10-20-background-results.md)
- [原生组合与主链接线](operations/evaluations/2026-10-20-native-composition.md)
- [重构后的性能回退](operations/evaluations/2026-10-20-runtime-parity.md)
- [评测方法](operations/memory-evaluation.md)、[评测程序与结果目录](../eval/README.md)
- [迁移规则](operations/workspace-migrations.md)、[订阅插件](operations/curated-feeds.md)
- [维护脚本与本地清理](../scripts/README.md)

## 开发参考

- [工程方法](engineering/method.md)
- [固定版本的 Akashic 工作流手册](engineering/akashic-vibe-coding-workflow.md)
- [记忆设计与证据表达](engineering/memory-design-and-evidence.md)
- [历史研究与验证记录](archive/README.md)

当前状态、功能验收和实测证据分别维护在上述唯一入口。历史记录保留版本和日期；
实验 JSON 的源码路径与哈希保持当时原样。新增文档应放入已有分类，并更新相关入口。
