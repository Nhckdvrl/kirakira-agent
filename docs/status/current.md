# 当前状态

Kirakira 使用同一个 Runtime 管理对话、记忆、工具、项目和后台执行。
模型由可替换的 driver 提供；Python、SDK 与评测使用同一个 `.venv`。

消息事实保留在 Ledger，图记忆和向量缓存可由完整来源恢复。
记忆具备语义召回、情景联想、原文引用、范围隔离和显式反馈；
具体能力是否有效，要看[当前验证](../operations/verification.md)中的实验条件与结果。
程序查询、Agent 工具和按出处展开原文已经接通；公开历史实验保留了强基线与失败。
图记忆尚未证明普遍优于向量，融合和片段模式保持显式选择。

事件入口支持 Content、Alert、Context，Wake 可以调查、分享、跳过并核对投递回执。
Drift 目前负责提案状态和结算，持续生成空闲提案的生产者仍需补齐。

[使用指南](../handbook/start.md) · [记忆与主动交互](../handbook/memory-proactive.md) ·
[插件清单](plugin-inventory.md) · [后续工作](roadmap.md)
