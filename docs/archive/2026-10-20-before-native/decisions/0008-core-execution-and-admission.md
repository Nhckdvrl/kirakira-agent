# 0008 共享执行器与运行日志

状态：accepted；日期：2026-10-03。依据：用户授权重构 core 与主要链路，并直接推送 main。

## 问题与决定

此前默认 runtime 已供被动、Wake 和 Drift 共享，但同步 Agent 仍自带循环，约两千行的 runtime 同时拥有上下文、模型传输、工具调用、对话提交与渠道调度。会话准入和中断快照位于渠道入口，直接/控制调用可能绕过。

将真实职责拆为 ConversationProgram、DefaultReasoner、ModelInvoker、ToolInvoker、PassiveTurnPipeline、TurnAdmission、AgentLoop 和 CoreRuntime。同步 Agent 仅为共享异步执行器的便捷入口；scoped 程序保持独立会话和提交语义。不是将旧实现另存后转导出；调用方改为依赖实际 owner。

对话 pipeline 拥有同会话排队、持久准入和取消快照；渠道调度只负责接收和调度。Runtime bootstrap 为共享 reasoner、Wake 和 Drift 注入同一个 ExecutionStore；子任务使用共享 reasoner，内部结果不进入记忆学习。

## 持久执行事实

`.kirakira/executions.db` 保存 run 的输入、父子关系、策略、模型请求与响应、工具意图和结果、终态。它是执行审计，不替代 Session 的消息事实或渠道送达收据。

工具开始前先提交 intent，完成后提交 result。取消、超时或进程退出留下未结算 intent 时，标记 `needs_reconciliation` 并停止继续推理；恢复不执行工具、不调用模型、不猜测外部动作是否发生。没有未结算工具的中断标记 `interrupted`。

Runtime owner 启动时结算旧 running 记录，清除旧会话准入锁；该启动路径要求 workspace 独占锁。原始执行事件不覆盖、不自动清理。只读检查器不能触发恢复：

```bash
python -m scripts.inspect_execution --workspace /path/to/workspace
python -m scripts.inspect_execution --workspace /path/to/workspace --run RUN_ID
```

模型在工具执行后拒绝上下文时，不能从头重试整轮；保留执行事实并显式失败。异步 streaming 取消后等待 worker 收束；同步 provider 的线程不能强制杀死，停止接收迟到 delta，仍受 provider 自身超时约束。

## 有意差异与代价

这不是 Akashic 的完整持久 Message 程序模型；不提供自动续跑、任意工具 exactly-once、跨进程 writer grants、全部后台 generation 生命周期或完整 supersession。执行日志确认模型/工具事实，不能确认尚未经过渠道 owner 的外部送达。

SQLite FULL 同步写入增加延迟，日志保存完整模型材料并占用磁盘。暂不引入自动保留期；后续要以真实历史规模测量写入成本，并在明确数据管理合同后设计归档。

与只搬动文件相比，选择同时修正共享入口的实际执行边界；与自动重试未结算工具相比，选择让不确定结果显式暴露，避免重复发送或写入。若工具具备稳定幂等键和独立查询结果接口，后续可以按工具合同增加恢复协议。

## 验证边界

回归覆盖真实本地 SQLite、共享推理器、模拟模型、工具调用与取消；检查 intent 先于副作用、崩溃恢复、不重放、直接调用中断、等待者取消与 scoped 日志。发送前必要副作用失败不得继续调用渠道。实际结果见[验证记录](../operations/verification.md)。

未执行真实收费 provider、外部渠道或线上 workspace 迁移。代码回退不撤销已经发生的工具副作用；新增日志文件回退后可保留。
