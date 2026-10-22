# 文档

## 使用

- [启动与日常使用](handbook/start.md)：单一 Python 环境、Web、模型切换、Telegram 和既有数据导入。
- [记忆与主动交互](handbook/memory-proactive.md)：记忆配置、事件入口、空闲思考和升级。
- [个性与相处方式](handbook/personality.md)：组合表达与行为偏好、人物底稿、临时要求及任务边界。
- [持续跟进](handbook/followups.md)：发布、网页与邮件接入，规则修改、复查和投递反馈。

## 设计与实现

- [产品定位与设计依据](architecture/design-direction.md)：个人 AI 伙伴的体验、设计取舍和实测依据。
- [人格与言行一致](architecture/personality.md)：14 个开源项目的设计输入、可组合人格、具体行为与验证方法。
- [Runtime 架构](architecture/overview.md)：统一执行主链、所有权、生命周期和恢复。
- [记忆架构](architecture/memory.md)：第二代学习循环、查询、原文证据和预算。
- [插件清单](status/plugin-inventory.md)：发行组件与默认选择。

## 开发与验证

- [工程工作流](engineering/development-workflow.md)：实现、验证、交付与目录维护。
- [维护脚本](../scripts/README.md)：源码校验、迁移、场景验证和清理命令。
- [评测复现](../eval/README.md)：环境、模型条件、真实链路与公开记忆实验。
- [伙伴与个性实测](operations/evaluations/2026-10-22-companion-personality.md)：单维度表达控制、示例与事实、日常话题接续及原文定位；功能与风格效果分开。
- [记忆场景亮点](operations/evaluations/2026-10-22-memory-scenarios.md)：从线索继续查原文、历史参与行动、出处复核；最终答案、开销和失败。
- [公开历史记忆质量](operations/evaluations/2026-10-21-memory-runtime.md)：逐题对照、补全贡献、真实主链与恢复证据。
- [验证结果](operations/verification.md)：分主题报告和原始证据。

## 当前状态与后续工作

- [当前状态](status/current.md)：实际交付与现有限制。
- [交付与研究路线](status/roadmap.md)：尚待验证的体验与研究问题。

带日期的评测报告记录当时版本，当前功能以使用说明、架构和状态页为准。
正式证据统一位于 `operations/evaluations/artifacts/<日期-主题>/`；历史实现和已完成的一次性迁移记录通过 Git 查看。
