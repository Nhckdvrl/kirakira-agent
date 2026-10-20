# Message 账本、Turn 与运行中插话

日期：2026-10-20。主链参考版本固定为
[`e1628ddc8a33c2840e5b4558ff3b66af207eec2a`](https://github.com/kachofugetsu09/akashic-agent/tree/e1628ddc8a33c2840e5b4558ff3b66af207eec2a)。

本轮改动实际接纳、模型步、工具结算、最终回复和恢复的读写路径。原生组合内核及其直接依赖
23 个文件与该版一致；TurnProjection 合同、实现及 frozen_json 原样移植，Message 值合同从
上游 ledger/contract.py 提取并移除未使用的导入。来源、许可证和摘要分别保存在
`agent/plugin_composition/UPSTREAM.json` 与 `plugins/ledger/UPSTREAM.json`。
Akasha2 的独立版本及 10 个算法文件未变，共校验 37 项摘要。

## 实现

| 环节 | 事实及提交边界 |
| --- | --- |
| 接纳输入 | `Input` 保存 origin、原文和媒体；与来源 head、程序和去重回执同一事务提交 |
| 模型批次 | 提出的调用先写 `Output(continue)`，固定工具名、参数及调用身份，再开始执行 |
| 工具结果 | `ToolResult` 用 `CallRef(message_id, part_index)` 指向已接纳调用；结果与效果状态同事务保存 |
| 最终回复 | `Output(complete/quiet)`、程序结算、聊天兼容投影及 Jobs 消费在同一 Session 事务提交 |
| 暂停和恢复 | 追加 `Control`；已知工具结果从消息引用恢复，未知外部效果仍需明确核对 |
| Turn | 使用上游 TurnProjection，返回正文与工具观察的引用，不另存内容或消费游标 |
| 显式删除 | 清理关联 Message 和程序恢复引用；运行中程序拒绝删除，删除失败则整个事务回滚 |

ReplyProgram 的持久记录不再内联输入、模型批次正文、工具结果或最终回复正文；它保留执行意图、
授权、lease、阶段和消息引用。`MESSAGES` 与 `TURN_PROJECTION` 是原生依赖图中的独立 capability，
回复程序与控制 provider 声明依赖，Application 的服务外观随同一图解析。

旧 `ledger_inputs` 和程序内联事实在 owner 启动时逐轮事务迁移。迁移按旧程序时间顺序处理，
不按随机输入 ID 排序；原输入、程序与聊天消息 ID 保留。更早的普通聊天及已闭合工具链也导入。
旧数据没有记录的并发时序不能从事后数据精确重建；导入顺序用于兼容，不冒充原始执行时间。

## 插话

Conversation owner 持有未接纳暂存，提供 `turn/steer` 和 `turn/withdraw`。
下一步模型请求前，在原程序的来源授权下追加 Input。已完成工具不因插话重复执行，
同轮多个输入保留独立消息身份。已接纳插话与结果可以跨重启显式续跑。
只有 conversation 来源可以绑定插话；Job 或 Scheduler 抢先取得同会话准入时，用户暂存
仍走原来的 conversation 后续请求，不会被接纳到后台来源。

若最后一次模型请求已经结束，暂存由 owner 接成普通后续请求；关闭 owner 时撤回未接纳暂存，
不会伪造已保存消息。`staged=true` 是暂存回执，`message/list` 出现 Input 才是耐久接纳。
`staged=false` 时调用方仍持有输入，可走普通 `turn/start`。
控制查询提供 `message/list` 和 `turn/project`，支持按 Session 与 seq 读取事实及逻辑 Turn。

## 当前边界

- 记忆、FTS 和 compaction 仍消费兼容聊天表；尚未统一为上游 Message Reader/Writer 授权合同。
- 同轮多用户输入的原文全部保留；现有记忆消费者只接受一问一答，所以这类 Turn 暂停自动学习，避免错误配对。
- steer 的运行时、控制协议及后续请求已接入；产品 UI 的暂存、撤回与重连交互仍需接入。
- 原生来源 Task、完整工具 binding/prepared、附件、HostBridge 和 Workload 合同仍未完成。

整体架构与全部功能尚未完全对齐；不能用本轮投影验收或固定内核摘要代替整体验收。

## 验证结果

在线请求全部使用 Step5 Token Plan 专用路由 `https://api.stepfun.com/step_plan/v1`，模型 `step-5-preview`，未切换现金路由。各组使用新临时工作区，沿用项目同一个 Python 3.14.6 环境，串行执行。

| 组 | 通过 | 原始记录 |
| --- | --- | --- |
| core | 36/36 | [core.json](../artifacts/2026-10-20-message-ledger/core.json) |
| scheduler | 13/13 | [scheduler.json](../artifacts/2026-10-20-message-ledger/scheduler.json) |
| workspace | 6/6 | [workspace.json](../artifacts/2026-10-20-message-ledger/workspace.json) |
| readout | 10/10 | [readout.json](../artifacts/2026-10-20-message-ledger/readout.json) |
| wake | 1/1 | [wake.json](../artifacts/2026-10-20-message-ledger/wake.json) |
| drift | 4/4 | [drift.json](../artifacts/2026-10-20-message-ledger/drift.json) |

最终在线组 **70/70**。主链 18 类场景、每类 2 次。插话与撤回经真实控制 Socket 进入；来源隔离强制交错用户回复、Job 和后续输入。Gate 只控制真实响应返回的时机，不替换模型响应。已接纳插话的取消/重启恢复保留原程序，工具不重放。

完整测试 **720 项与 4 个 subtest 通过**。新增回归沿用原测试文件；22 个警告来自原有 Telegram 弃用提示。打包、CLI、锁文件、功能清单及 37 项固定源码摘要通过。wheel/sdist 中 13 个关键文件逐字节核对，生成包及归档结果的凭据扫描通过。见 [checks.json](../artifacts/2026-10-20-message-ledger/checks.json)、[unit-final.log](../artifacts/2026-10-20-message-ledger/unit-final.log) 与 [source.json](../artifacts/2026-10-20-message-ledger/source.json)。

已计量的五组观察到 170 次模型尝试（包含故障注入），162 次响应完成；上报 total_tokens 为 271,114。其中 168 次有耗时，p50/p95 为 1.95/7.38 秒。Drift 未逐请求计量，Wake 未计时，不能把这些数字包装成覆盖全部请求的统计；取消响应还可能缺失 usage。37 段保存的回复检查了工具调用文本标记，未把文字伪调用当作实际执行。这些都是合成功能场景，不是外部任务成功率或供应商速度评测。

## 压力检查与性能边界

两次固定千轮均为 64 个会话、5000 次工具调用、1000 次效果及 77 次注入重试；未结算记录均为 0，重启均恢复 2000 条聊天历史。模型使用确定性替身，网络调用为 0，不能与 Step5 在线延迟混算。

| 采样 | 吞吐（轮/秒） | 结果 |
| --- | --- | --- |
| 1 | 24.86 | 1000/1000 |
| 2 | 25.71 | 1000/1000 |

本轮中位数 25.28 轮/秒，比上一轮单点 54.16 低约 53.3%。**性能没有保持旧水平，尚未恢复。** 这是同机网络挂载盘上的固定场景，尚未分解日志、兼容投影和环境各自的成本。本轮完成消息架构与功能接线，没有以吞吐提升作为完成条件。原始记录为 [pressure.json](../artifacts/2026-10-20-message-ledger/pressure.json) 和 [pressure-repeat.json](../artifacts/2026-10-20-message-ledger/pressure-repeat.json)。

## 中间失败与复测

- 两轮中间主链分别为 33/34、35/36，各有一次插件/MCP 启动握手超时，均为 0 次模型请求；独立运行最终为 36/36。原始失败和日志保留。
- Scheduler 中间为 11/13。两个临时目录的 Unix Socket 路径为 112/115 字节，实际 bind 复现 `AF_UNIX path too long`；短路径整组 13/13，未修改生产超时。见 [路径诊断](../artifacts/2026-10-20-message-ledger/scheduler-path-diagnostic.json)。
- 初次完整回归的 4 个失败来自新增迁移未加入测试固定清单；一次装配测试发现 Application 缺少新 facade 字段。已修正，最终完整回归通过，原始失败日志保留。

启动握手超时仍是本次观察到的稳定性边界，最终成功复测不会抹去此前失败。汇总见 [summary.json](../artifacts/2026-10-20-message-ledger/summary.json)。

复测使用较短的临时路径：

```bash
uv run --frozen python -m eval.scenarios.core \
  --key-file "$STEP_PLAN_KEY_FILE" \
  --workspace /tmp/kml-core-new --output /tmp/kml-core-new.json \
  --repeats 2 --concurrency 1
uv run --frozen pytest -q
```

Key 文件仅供本机读取，不写入配置、结果或仓库。每次使用新工作区和输出路径，保留先前记录。
