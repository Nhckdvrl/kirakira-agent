# 当前状态

更新：2026-10-20。

运行架构采用 Akashic `e1628ddc8a33c2840e5b4558ff3b66af207eec2a` 的完整原生实现：
宿主/插件管理、Context/Fiber/Effect/Task、Ledger、Sources、Reply/ReplyProgram、
Context/Models/Tools、HostBridge/ManagedProcesses/MCP/Workloads，以及业务插件。
`UPSTREAM.json` 固定 924 个生产文件，其中一处 zsh 运行修复有独立补丁记录。

Kirakira 使用这一条主链。旧 ProgramStore、自建 admission、分离的 execution facts
和兼容 SessionManager 已移除。Python 环境统一为项目 `.venv` 的 3.14.6。

[架构](../architecture/overview.md)说明实际所有权与调用链；
[启动和迁移](../handbook/start.md)说明新配置格式、旧数据导入和 Step5。
原有数据未在开发过程中迁移或覆盖。

代码移植覆盖全部上游生产插件。外部模型账户、Telegram、容器 controller、浏览器等
可选提供方需要各自配置；未执行的外部集成不能由源码摘要或离线测试推断为已验收。
当前验证记录将明确列出实际通过范围，历史性能结果不作为本次架构完成的证明。

## 验证

完整合同测试 88 通过；最终 Step5 主链两轮 26/26、工作台与项目端到端 1/1。
生产前端构建和 TypeScript 检查通过。原始失败、真实调用计数及范围见
[本轮验证](../operations/evaluations/2026-10-20-native-architecture.md)。
