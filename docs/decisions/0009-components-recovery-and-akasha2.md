# 0009：组件能力、生成恢复与可比较的 Akasha2

日期：2026-10-19。源码参考固定为 Akashic
[`d85b00c233686b68b082052b1b20123905aeab13`](https://github.com/kachofugetsu09/akashic-agent/tree/d85b00c233686b68b082052b1b20123905aeab13)。
本次实现参考其 ADR 0092、0100、0102、0112、0118、0125；这些文档的状态不等于所有上游部署均已验收。

## 组件与代际

`agent/composition` 只拥有能力发布、借用和资源寿命，不解释记忆、订阅或主动业务。
提供者定义有版本的 `ServiceKey`，消费者在 `Plugin.requires()` 声明依赖，通过
`async with context.use(key)` 借用。发布按依赖顺序进行，不隐式安装或寻找提供者。
同名能力的 Python 合同类型在进程内保持稳定；改变 ABI 必须换版本。

一个借用固定实现与 generation，换代只影响后续借用。旧代际租约归零后先回收其
`ResourceScope`，再 terminate。scope 拥有通过 `context.spawn` 创建的任务与
`context.on_close` 登记的资源；取消调用者也必须等待任务 finally 和物理资源关闭。
插件仍是可信的进程内 Python，不是沙箱。prepare 应构造贡献，不能假定所有依赖已发布。

Plugin API 升至 3。删除 `PluginContext.session_manager`、`.memory` 两个宽泛入口；
记忆能力合同在 `plugins/default_memory/contract.py`，具体构造在 `bootstrap/memory.py`。
删除原 `core/memory/services.py`，不保留双工厂。每个对话 turn 持有自己的记忆能力租约。
`CoreRuntime.service_registry.catalog()` 可查看能力 owner 与代际，不暴露实现对象。
内置工具、完整记忆协议和部分维护流程仍由现有组合根装配，不宣称完成上游全部业务插件化。

## 模型生成恢复

provider 一次调用对应一次物理请求；`ModelInvoker` 独占逻辑请求的重试策略。
默认最多 10 次重试，即 11 次物理尝试。只处理明确的传输失败、429/5xx 与超时；
权限、上下文、内部程序异常不能伪装成可重试网络故障。
服务器 Retry-After 优先，否则从 0.5 秒指数增长到 32 秒并加 0–25% 抖动。
等待可取消，达到上限产生明确终态。

同一次逻辑生成冻结消息、工具和参数。失败片段发 reset 事件清空，下一尝试重新生成；
TUI 展示次数、等待与恢复。终止原因缺失的 SSE 不算完整回复；长度截断不得执行工具。
已完成的工具结果保持在消息和执行事实中，重试不能重跑整个 ReAct 任务。
进程崩溃后的未知外部工具结果仍需核对，不自动重放。它与单次生成的网络重试是不同合同。

Step Plan 专用客户端只接受官方 HTTPS `/step_plan/v1`，禁止跨计费面的重定向；
Step5 实际模型 ID 是 `step-5-preview`，原生推理控制为 `reasoning_effort`；不发送未获文档支持的 `thinking` 字段。验证器不读取私人 workspace 配置，不回落到现金 API。

## 两条主动链

Wake 可启用兴趣池：每个候选修订、模型与用户规则/长期记忆指纹只保存一次初始兴趣估计。
模型提交 interest、confidence、eligible 与理由，提交不是发送授权。
候选质量平方按 18 小时半衰期衰减；池质量达阈值且存在待处理到达，才进入 share/skip。
多来源排序降低同源内容的展示权重，不改变池总质量。低于阈值记录 pool_wait，等待新到达；
同一内容不会靠高频 tick 反复打分。候选更新、会话变化与实际投递继续使用原版本/CAS 边界。
旧配置保持显式兼容，配置示例启用新池；外部内容质量和打扰体验仍需要用户标注评测。

Drift 增加持久 proposal：记录 skill 内容摘要、原会话 head、briefing、理由、优先级与状态。
执行前 claim；会话或 skill 版本变化使排队提案失效；同一 runner 串行选择与执行。
运行中的任务重启后变为 interrupted，进入投递的任务变为 needs_reconciliation；均不自动续跑。
草稿只有 finish 且会话未变化才能投递。执行事实使用 proposal ID 关联。
当前提案来自显式安装的技能与空闲调度，还没有实现模型自行发现目标的完整 proposal planner。

## Akasha2 与证据

`plugins/akasha2` 接入固定版本的纯 MemoryCycle、特征、扩散、图学习、readout 和 SQLite 快照。
原文件清单、SHA-256、MIT 许可证随包分发；Kirakira 的源消费与恢复 adapter 独立维护，
没有从 Reference 目录动态导入。保留 v1，并通过 `[memory].plugin` 显式选择新引擎。

原始消息仍是唯一事实源。持久 publication 顺序驱动学习；通知只唤醒消费者。
首次启用记录 cutover；历史必须显式重建。完整消息对、原始内容摘要、消费位置、向量空间和图
一起原子发布。默认每 16 个 publication 以及正常关闭时发布；未发布尾部重启后重新消费。
恢复校验原文、消息顺序与模型空间。发布失败或学习可能部分失败后禁止继续使用内存图。
独占 writer lease 防止两个消费者覆盖同一快照。计算在线程中完成，取消仍等待物理计算结束。

查询不学习，返回原始消息 ID 与 source_ref；不能静默把有未来学习状态的图当历史快照查询。
Akasha2 不公开未实现的 remember/forget 或管理接口。`MemoryEngine` 不再强制继承 Dashboard Admin。
新增离线固定向量重建命令，不访问网络、不改来源、不覆盖、不自动激活旧数据库。
本次没有迁移用户真实历史库。

## 验证

验证由真实 loopback HTTP/SSE 故障、原始 SQLite 消息消费、正常/崩溃快照一致性、
组件借用/清理、提案恢复，以及 Step5 在线运行组成。确定性 hash 向量只用于合同回归。
质量实验单独使用固定公开数据、本地 BGE 向量、相同上下文预算和 Step5 reader/judge，
保存逐题输出、错误、请求 usage、数据/代码/模型摘要。结果与边界见
[验证记录](../operations/verification.md)及[记忆评测](../operations/memory-evaluation.md)。
