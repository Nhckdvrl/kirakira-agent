# 0014：主链工具并发与物理排空

状态：accepted；日期：2026-10-19。参考 Akashic `21b38f0` 的 reply_program、tools、
来源检查及 [0079](https://github.com/kachofugetsu09/akashic-agent/blob/21b38f0e102d9e1bdc5576725635dce29f135c0c/docs/decisions/0079-parallel-tool-calls-commit-in-model-order.md)、
[0100](https://github.com/kachofugetsu09/akashic-agent/blob/21b38f0e102d9e1bdc5576725635dce29f135c0c/docs/decisions/0100-message-tool-calls-recover-at-turn-granularity.md)。

## 工具批次

注册表新增 `parallel_safe=True`，默认关闭，且必须声明 `risk="read-only"`。风险标签本身不自动
开启并发；MCP、第三方工具与未知工具默认串行。当前内置提供者只为 read_file/list_dir 开启。
调用方可用 `AGENT_MAX_PARALLEL_TOOLS` 调整上限，默认 4，设为 1 恢复串行执行。

`tool_batch.py` 只调度连续的显式安全读取。空槽立即供后续读取使用；未声明的工具、写操作、
tool_search 与终结工具都是屏障。每个调用仍经过共享 ToolInvoker 的授权、hook、intent 与 result。
并发任务显式借用父 execution，并 fork 当前快照租约；普通自行创建的 task 仍不能隐式取得授权。
工具结果及 tool_chain 按模型调用顺序进入后续上下文，慢读取不会被更快的后继调换位置。

执行日志保留真实完成顺序；它是审计事实，尚不是上游 Ledger 中逐条持久提交的 Message 程序。
因此不声称已移植上游的顺序提交事务。主链 Session 仍在整轮提交；未知副作用仍停止自动重放。
并发仅作用于读取，不能借此推断任意工具可以 exactly-once 或安全重试。

## 取消与关闭

Python 无法强制杀死同步工具线程。`run_sync_drained` 在取消后保留会话与代际所有权，等实际线程
结束才向调用者返回取消；重复取消不能提前释放。执行结果不因线程终于结束而猜测为成功，未结算
intent 继续标记 needs_reconciliation。工具 owner 仍需为可能无限等待的外部调用提供自身超时。

模型 worker 使用一次取消和 shield 排空，避免第二次取消打断 provider 的 finally。非流式与流式
共用这一边界。流结束通过完成哨兵唤醒，替代固定 100ms 轮询；已排队 delta 仍按顺序交给观察者。
同步模型 provider 的底层线程仍受 provider 自身超时约束，不能由 asyncio 强行终止。

TurnAdmission 跟踪所有入口的 active/waiting 参与者。Runtime 先停止接收，再关闭 admission，
排空直接、渠道、控制与 Scheduler 调用后才关闭工具和数据库。关闭后拒绝新输入；队列中未获准的
请求不写入幻影消息。关闭方法不能由自身 admitted turn 同步调用。

## 中断后的新请求

原始消息中的 `interrupted=True` 是控制事实。ConversationProgram 在下一请求的 system section
中明确说明旧轮已经关闭，优先执行最新输入，只有用户显式要求继续时才恢复旧任务；已发生的效果
不会因中断撤销。普通消息文本中的 `[interrupted]` 不具备这项权限。
提示从持久元数据恢复，不改写原文，不声称提示能保证模型永远遵从。

## 日志查询

未结算检查按 run/kind 索引只读取工具事件。Python 只解码 intent，结果只提取 operation_id，
不再为每次检查解码整轮模型上下文和大结果正文。ToolInvoker 用本调用的写入收据确认结算，避免把
仍在执行的兄弟调用误判为未知结果；run 结束和启动恢复仍从原始日志检查全部未结算操作。
已有数据库只增加可重建索引，不改写旧事件或既有 Yoyo migration。

未配置 context_window 时不创建新的压缩器，也不复制整段历史作无效压缩检查。已有摘要的读取投影
仍正常使用，原文与学习游标保持原状。该优化通过千轮逐请求摘要等价检查；有窗口时保留原压缩路径。

验收与实际数据见[主链实验报告](../operations/evaluations/2026-10-19-core.md)。
