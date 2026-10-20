# Kirakira 与 Akashic 的差距与实施清单

核对日期：2026-10-20。Kirakira 起点 `a80a7d1`，原固定上游 `e1628dd`；
本轮参考上游 `d8761bb9000a7be8701c65e049b0978879cc6889`。
比较范围包括源码、发行组合、实际可用服务、恢复路径和可复现验证；不以文件存在证明功能已经接通。

## 结论

CompositionRoot、Context/Fiber/Effect/Task、Ledger、Sources、Reply/ReplyProgram、
Context/Models/Tools、执行设施与原生插件沿用同一个上游实现。
主要差距在最新合同修复尚未同步、发行默认组合、主动流程输入与目标、记忆 embedding，
以及验证和文档。作者的 private_runtime 子模块不在公开主仓中，不能把私有渠道或账号记作已复刻。

| 优先级 | 差距 | 起始证据 | 本轮交付与验收 |
| --- | --- | --- | --- |
| P0 | 最新原生合同尚未同步 | 上游已修改来源未结束时的压缩边界、消息目录读取、插件公共合同缓存、工作台加载 | 同步固定提交，重建界面，验证未结束来源和已存在消息完整性 |
| P0 | Akasha 与兴趣评分没有可用 embedding | 已迁移工作区只有 Step5 chat，记忆验证仅覆盖 Markdown | 接入真实本地 embedding 驱动，保持 Step5 Token Plan 为唯一远端模型，验证学习、召回、反馈、重启和离线向量复用 |
| P0 | Wake 缺少可运行的接入路径 | 默认 delivery 为空，没有面向本地集成的 EventMail 输入入口 | 通过原生 EventMail 来源合同接入事件，显式配置本地投递目标，验证发送、跳过、重投、重启和回执 |
| P1 | 默认组合缺少运行诊断和 Agent 插件安装入口 | runtime_diagnostics、plugin_update 源码存在但未被发行配置选中 | 接入正式组合并通过实际服务/工具和诊断采样验收 |
| P1 | 量化范围窄，部分旧结果对应已删除实现 | 当前在线主链覆盖 13 类；记忆和双主动的主要量化结果来自旧主链 | 新原生图中记录功能成功率、重复效果、恢复一致性、模型调用及 token；保留失败结果和计数分母 |
| P1 | 文档入口仍混合退役架构和旧计数 | docs/archive、旧 evaluations 与 artifacts 保留已退役合同；verification 仍称 88 项 | 删除过期说明和重复日志，重写当前索引、操作手册、验证入口，检查内部链接 |
| P2 | 外部设备、账号、容器与私有插件未验收 | 公开源码或协议场景不能证明真实渠道/设备可用 | 单独列出需要的实际提供方；本轮不伪造外发、设备或容器结果 |

## 实施顺序

1. 固定并同步上游生产代码，保留 Step Plan、迁移和发行边界；不建立第二套消息、任务或效果执行器。
2. 补齐本地 embedding 与原生主动流程接入，接通默认诊断和插件扩展能力。
3. 在独立本机工作区运行真实 Step5 功能矩阵及量化评测，按证据回填本表。
4. 删除退役说明，保留当前架构、使用手册、差距清单与有效验证记录；历史恢复点使用 Git。

## 验收边界

本地 embedding 不产生现金 embedding API 请求。向量检索、图召回和 Markdown 读取分别报告；
固定小样本或合成资料不冒充 LongMemEval 全量成绩。发送成功以原生 Delivery 回执为准，
持久调用去重与模型重新提议相同动作分开统计。未结束来源不能被摘要吞掉，
恢复必须核对已有消息身份、正文、顺序与已提交效果。性能数字用于测量，不改写业务规则取绿。

本轮核对与执行结果见下文及[完整验证记录](../operations/evaluations/2026-10-20-alignment.md)。

## 实际工作区补充发现

- 已提交的原生 selection 是启动权威。更新目录和启停列表不能证明既有工作区已选入新提供方。升级入口现通过原生 prepare/CAS 固定当前内置输入，备份后一次提交，保留市场安装输入与用户其他选择。
- 原生 reindex 默认跳过无缓存向量的历史。发行层新增显式本地 backfill，写入同一 Ledger 派生缓存，再由原生 Akasha 重建。没有第二套图或消息库。
- 空图重建原本被“turn_count 必须为正”错误阻止；原生代码本就有零节点发布分支。已登记一处最小校验补丁，并增加正式命令入口的零节点场景。
- 构建产物需进入所属插件制品；已修复 staging。wheel 原本漏打包 bundles / assets / schema 等运行资源；已修复 namespace 数据包含并独立启动包内 Runtime 验证。

## 整体分层核对

下表的“对齐”指固定公开提交的实现来源与所有权一致；外部服务和设备另需运行验收。
核对依据为实际 Git tree、插件静态声明、发行组合和原生合同，不只看 README。

| 层 / 组件 | 目前与上游的关系 | 功能证据或剩余边界 |
| --- | --- | --- |
| 宿主与插件生命周期 | 同源 PluginManager / Root / Context / Fiber / Effect / Task | 启停、局部更新、固定公共合同、撤权与物理排空；无自建并行内核 |
| 来源接纳与消息 | 同源 Sources、Ledger、TurnProjection、OwnerState、Bindings | 唯一事实日志；旧数据已显式导入，历史执行授权不自动恢复 |
| 回复主链 | 同源 Reply / ReplyProgram / ReAct / Context / Compaction | 持久工具调用、来源抢占、steer / pause / resume、摘要边界 |
| 模型与工具 | 同源 Models、菜单、prepare / authorize / execute | Step Plan 是本地发行 driver；不是另一个工具执行器 |
| 执行设施 | 同源 HostExecution / ManagedProcesses / MCP / Workloads | 本地进程、Shell 和 MCP 可验；Workload 的真实容器控制器未部署 |
| 记忆 | 同源当前 Akasha，而非保留旧 Akasha 分支 | 本轮补齐中文本地向量空间、正式启用配置；图与消费快照不另行实现 |
| 显式档案 | 同源 Markdown Memory | 原档案、迁入资料、更新恢复各有原生归属 |
| 事件主动 | 同源 EventMail / Wake / DeliveryPolicy / Delivery | 本轮补齐接收事件与本地目标；来源评分与实际送达分开报告 |
| 空闲自治 | 同源 Drift 提案、领取与结算合同 | 缺可持续生成提案的真实生产者；不得宣称“双主动全流程已验收” |
| 计划任务与子代理 | 同源 Scheduler / Jobs / Subagent / Programmatic | 由正式主链执行，已纳入 Step5 功能矩阵 |
| 多渠道 | 同源公开渠道插件与 Gateway | 原生 akashic 本地渠道可验；Telegram、外部发送等需账号，未验收 |
| 工作台 | 同源 dashboard / chat / ui / web-auth / plugin UI | 本轮同步 Vite splash 与 importmap 顺序修复，需构建、类型和 HTTP 检查 |
| 项目与工作目录 | 同源 Projects / WorkingDirectory / Shell UI | 会话 scope、相对路径、迁入目录与项目隔离有正式入口 |
| 插件与 Skill / 扩展 | 同源安装更新机制；本地发行 Skill 另行维护 | 本轮开启 plugin_update，并用 Step5 实际安装 Git 插件；上游开发/验证 Skill 模板尚未全量引入 |
| 诊断 | 同源 runtime_diagnostics / runtime_inspection | 本轮选择并验证采样；未用改业务规则追逐延迟数字 |
| SDK 与交付 | 同源 Python SDK，打进同一发行包与同一个环境 | 不另建 Python 环境；本地 wheel 与入口要独立检查 |
| Docker 与 systemd | 上游另有完整 standalone、controller、computer 等部署资产 | 当前 Kirakira 仍是本机发行，不具有同等“一条 Compose 启动”交付；列为后续 P1 |
| 实验 harness | 上游 benchmark 另含 Harbor / computer driver | 当前受控评测与原生场景不能等同 OSWorld / Harbor 成绩；列为后续 P1 |
| 私有运行环境 | 上游 private_runtime 是指向另一仓库的子模块 | 公开主仓不能提供私有集成实现、数据与账号；不可假定与作者实际实例相同 |
| 文档与工程规则 | 维护自己的当前文档和精简验证集 | 不机械复制上游全部测试、历史计划、作者个人工作流与 CI 账户配置 |

### 后续顺序

1. 为 Drift 选择真实、受控的空闲任务来源，沿原生提案与 Wake/Delivery 合同完成生成、执行、投递和重启验证；先定义退出条件和扰动预算。
2. 形成适配 Kirakira / Step Token Plan 的独立部署交付，接入真实容器 controller，再验收 Workloads 与设备环境。
3. 将工程插件开发/验证 Skill 模板与当前本地说明统一；扩展评测到规模更大、可公开复现的原始对话资料及上游实验 harness。
4. 外部渠道与私有服务按实际需求逐个配置和验收，不能用源码清单代替结果。

本地三个发行插件的责任分别是固定 Step Token Plan 路由、本地 embedding 资产适配、
原生 EventMail 的认证入口。它们不改变 Agent core 的所有权或持久执行合同。

上游证据：[固定提交](https://github.com/kachofugetsu09/akashic-agent/tree/d8761bb9000a7be8701c65e049b0978879cc6889)、
[Drift 插件](https://github.com/kachofugetsu09/akashic-agent/blob/d8761bb9000a7be8701c65e049b0978879cc6889/plugins/drift/plugin.py)、
[部署入口](https://github.com/kachofugetsu09/akashic-agent/blob/d8761bb9000a7be8701c65e049b0978879cc6889/compose.yaml)。

## 本轮完成情况

- P0：同步固定上游原生修复；刷新既有工作区的已提交内置输入。930 个固定本地文件覆盖 929 个唯一来源，未登记漂移为 0。
- P0：接通真实中文本地 embedding、Akasha / 兴趣评分、旧历史补算与原生重建。实际图含 61 个闭段，126 条既有事实全部保全。
- P0：接通认证 Gateway 的 Content / Alert / Context 入口、本地 Wake 目标、核实后分享、真实送达、去重、ack 与重启。
- P1：默认选入诊断与插件安装；Step5 实际安装并激活 Git 插件。
- P1：修复前端 staging 与 wheel 运行资源，包内 Runtime、SDK、真实 Step5 与认证工作台分别验证。
- P1：删除 126 份退役说明和旧评测产物，统一当前文档、操作方法及可复现证据。

受控记忆中 Dense 与 Akasha 均为 Hit@3 / Exact Match 12/12；无记忆为 Exact Match 0/12。
这是链路验收，尚无相对 Dense / Mem0 的优越性证据。详细分母、预算和失败过程见
[本轮验证](../operations/evaluations/2026-10-20-alignment.md)。

仍需补齐的是 Drift 空闲提案生产者、独立 Docker 交付和真实容器/设备/外部渠道，
以及规模更大、关联与时间推理更强的公开记忆评测。这些没有被源码一致性或小样本正确率代替。
