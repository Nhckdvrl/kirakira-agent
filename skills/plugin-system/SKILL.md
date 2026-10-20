---
name: plugin-system
description: 开发、安装、更新或排查原生 Kirakira 插件时使用。
---

# 原生插件

扩展使用 api_version = 3 的模块式 plugin.py，静态声明 name、version、
inject，以及需要的 workspace_roots、workspace_files、entrypoints。
入口为 apply(ctx) 或 run(ctx)。公共能力从 contract.py 导入，
通过 Context 提供或取得，不使用退役的 Plugin 子类或 phase 钩子。

先阅读 agent/plugin_composition 与目标能力合同，选择最小依赖集。
工具授权、来源任务、效果回执分别属于 Tools、Sources、Ledger/Jobs；
扩展不能创建第二套会话存储，也不能绕过持久调用身份。

参考 eval/fixtures/native_extension/plugin.py 验证真实工具、MCP 和进程。
制品应自足，不能跨插件导入另一个插件未公开的实现。

安装前查看 python main.py --help，再使用 plugin-install、plugin-status、
plugin-uninstall 管理正式安装。核对 accepted、selected、active 和代际，
已接收不代表已经生效。不手写旧 manifest.toml。

开发 checkout 的发行插件位于 plugins/，默认安装目录为 ~/.kirakira/plugins。
业务数据和配置属于选定工作区中的 plugin-data/。卸载代码时保留用户数据、
凭据和回退路径。具体工具和 CLI 参数以当前运行时实际公布的合同为准。
