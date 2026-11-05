# 文档

先看[证据与验证](operations/verification.md)：那一页先讲本项目最值得称道的四件事（可取证的记忆、可纠正的主动判断、说得出并降下来的开销、可核对的来源边界），每件都带数字，后面是全部 13 份报告的索引。

## 使用

- [启动与日常使用](handbook/start.md)：单一 Python 环境、Web、模型切换、Telegram 和既有数据导入。
- [记忆与主动交互](handbook/memory-proactive.md)：记忆配置、事件入口、持久目标、空闲思考和升级。
- [个性与相处方式](handbook/personality.md)：组合表达与行为偏好、人物底稿、临时要求及任务边界。
- [持续跟进](handbook/followups.md)：聊天交办与工作台管理，发布、网页与邮件接入，规则修改、复查、投递反馈与关注权重。

## 设计与实现

- [产品定位与设计依据](architecture/design-direction.md)：三条特色的机制与取舍，以及读过的设计输入。
- [记忆架构](architecture/memory.md)：学习循环、两条检索通道、原文证据、窗口规则与开销。
- [Runtime 架构](architecture/overview.md)：统一执行主链、所有权、生命周期和恢复。
- [人格与言行一致](architecture/personality.md)：14 个开源项目的设计输入、可组合人格与验证方法。
- [插件清单](status/plugin-inventory.md)：发行组件与默认选择。

## 开发与验证

- [证据与验证](operations/verification.md)：四项主要结论与全部报告索引。
- [评测复现](../eval/README.md)：环境、模型条件、开销测量与质量对照的完整命令。
- [工程工作流](engineering/development-workflow.md)：实现、验证、交付与目录维护。
- [维护脚本](../scripts/README.md)：源码校验、迁移、场景验证和清理命令。
- [第三方来源声明](../third_party/THIRD_PARTY.md)：上游来源、逐文件摘要、采纳与拒绝记录。

## 当前状态与后续工作

- [当前状态](status/current.md)：实际交付与现有限制。
- [交付与研究路线](status/roadmap.md)：尚待验证的体验与研究问题。

带日期的评测报告记录当时版本，当前功能以使用说明、架构和状态页为准。正式证据位于 `operations/evaluations/artifacts/<日期-主题>/`；历史实现与已完成的一次性迁移通过 Git 查看。
