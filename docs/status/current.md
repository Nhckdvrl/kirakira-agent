# 当前状态

更新：2026-10-20。原生架构固定 Akashic
`d8761bb9000a7be8701c65e049b0978879cc6889`。

929 个本地固定生产文件覆盖 928 个唯一上游运行来源，运行来源无缺失、未登记的漂移为 0；zsh 修复、空图重建校验、发行构建 staging 和生成资产分别登记。
不参与运行或构建的上游联系二维码已排除，逐项记录于来源清单。
70 个公开原生插件沿用同一个宿主与业务实现；Kirakira 增加 Step Token Plan、
本地中文 embedding 和原生 EventMail 入口三个普通插件。
源码校验、默认启用和功能验收的区别见[差距与实施清单](akashic-gap.md)。

主链为 PluginManager / CompositionRoot / Context / Fiber / Effect / Task、
Ledger / Sources / Reply / ReplyProgram / Models / Tools 与正式执行设施。
Python 和 SDK 使用项目同一个 `.venv` / 3.14.6。

新工作区默认启用诊断、插件安装、事件入口与本地模型 driver；既有工作区显式 upgrade。
本地记忆模型和 Wake 投递目标分别通过正式配置接入，操作见
[记忆与主动交互](../handbook/memory-proactive.md)。

公开 Drift 插件拥有提案状态和结算，当前仍缺持续生成空闲提案的生产者。
Docker 一键发行、实际容器/设备、外部账号与公开规模 benchmark 仍需后续补齐，
不能将全部源码存在理解为作者所有私有运行环境已复制。

## 数据与验证

既有数据已迁入原生本机工作区：112 条原始消息、22 个会话、2 条 Markdown 记忆和 6 个 Skill。
原始数据与私有备份保留；[迁移记录](../operations/evaluations/2026-10-20-workspace-migration.md)
用于恢复该次数据切换。

当前合同测试 93 项通过。在线功能、受控记忆对比、主动交互和原生恢复场景的
具体分母、调用范围和原始结果见[当前验证](../operations/evaluations/2026-10-20-alignment.md)。

实际工作区本轮已补算 118 条本地向量，由原生命令重建为 61 个闭段。
本轮之前的 126 条事实逐条保持一致；74 份持久文件已有私有可恢复备份。
Step5 主链 13/13、主动与反馈 6/6、认证工作台 1/1；受控记忆两种检索均为 12/12，
不将其表述为相对其他系统的性能或质量优势。
