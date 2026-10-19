# 后台结果链路与 Step5 验证

日期：2026-10-20。上游 main 固定为
[`6016befc682a037fbfc61180d39f17c219572ed6`](https://github.com/kachofugetsu09/akashic-agent/tree/6016befc682a037fbfc61180d39f17c219572ed6)。

原生 Context/Fiber/Effect 内核及直接依赖的 **23 个文件与该版本逐字节一致**，
另 10 个 Akasha2 文件继续按记忆自己的版本固定。验证原始摘要在
[upstream-kernel.json](../artifacts/2026-10-20-background-results/upstream-kernel.json)。
整体架构尚未完全对齐：统一 Message 日志与投影、完整材料协议、steer/撤回、原生 Task 和执行宿主
仍未完成，具体边界见[架构范围](../../architecture/overview.md#与当前上游的架构范围)。

## 本轮实现

新增独立 `plugins/jobs` owner，子代理只发布后台结果，不再直接把完成通知塞成一条用户消息。

| 环节 | 实际行为 |
| --- | --- |
| 发布 | 结果和原会话/渠道一起持久保存；重复发布幂等，身份复用但结果或路由不同则拒绝 |
| 忙时 | 回复程序在 ReAct 模型步前领取结果，作为当前程序的材料；请求重试继续使用冻结输入 |
| 闲时 | Reply follower 进入原会话 admission 后再检查，已经被当前轮领取的结果不另起回复 |
| 提交 | 最终消息、程序 completed 和结果 consumed 在同一个 SQLite 事务提交 |
| 中断 | 取消或模型拒绝不提前消费；原程序恢复后仍能取得同一结果 |
| 重启 | 已接纳、尚未领取材料的唤醒复用原程序；已领取的暂停程序等待显式恢复，保留效果和历史 guard |
| 查询 | `spawn_manage(action="results")` 按当前会话读持久结果；list/cancel 同样限制会话范围 |

删除重复的 `subagent-runs/result.json` 写入。Session 删除同时清理结果与消费回执；Scheduler 和
自治来源不会顺便消费用户的后台结果。独立 `job:<id>` 回复不增加用户消息，默认排除记忆学习。

局部替换补齐了真实注册生命周期：Session owner 钩子可按 activation 撤销；子代理只注入 Jobs，
不持有总线。子代理和 Scheduler 的工具先暂存、再按原生 activation 发布和撤销。
回归核对 Jobs 替换、Ledger 身份保持、结果保留，以及旧 disposer 不误删新钩子；在线用 Step5
验证替换后仍能取得并消费原结果。

这里的 consumed 表示：模型请求包含了该材料，且回复已在本地保存。它不代表模型语义必然正确、
外部渠道已经送达或收件人已读。原始结果仍可通过 results 查询。

## 最终在线验证

所有在线模型调用固定为 **Step Token Plan `/step_plan/v1` + `step-5-preview`**，无现金账户路由、
模型降级或其他 provider。使用隔离 workspace，记忆学习关闭；Wake/Drift 使用合成来源和本地投递回调。
凭据只从仓库外 key file 读取。

| 场景组 | 通过 / 总数 | 证据 |
| --- | --- | --- |
| 主链与后台结果 | 30 / 30 | [core-final.json](../artifacts/2026-10-20-background-results/core-final.json) |
| 失败结果读出、流取消后重启恢复，各重复 5 次 | 10 / 10 | [readout-final.json](../artifacts/2026-10-20-background-results/readout-final.json) |
| Scheduler、效果去重、未知结果、来源替代与历史隔离 | 13 / 13 | [scheduler-final.json](../artifacts/2026-10-20-background-results/scheduler-final.json) |
| Project、工作目录与重启 | 6 / 6 | [workspace-final.json](../artifacts/2026-10-20-background-results/workspace-final.json) |
| Wake：送达、去重、反馈 | 1 / 1 | [wake.json](../artifacts/2026-10-20-background-results/wake.json) |
| Drift：静默、送达、来源失效、暂停 | 4 / 4 | [drift.json](../artifacts/2026-10-20-background-results/drift.json) |
| 合计 | **64 / 64** | [summary.json](../artifacts/2026-10-20-background-results/summary.json) |

主链组包含并发读取、工具执行后的传输重试、deferred 加载、工具错误修复、文件与压缩重启、
provider 局部替换，以及真实 Step5 子代理读取文件并把结果合并进父程序。

最终组记录 **150 次调用尝试、139 次成功模型返回**，其余包含受控取消和传输故障注入，不能把这些
故障注入算成 11 次业务失败。已报告用量合计 252,455 tokens；取消流的用量可能未报告。
已完成请求延迟 p50 **2.58 s**、p95 **7.95 s**。主链整项延迟 p50 **8.35 s**、p95 **16.89 s**，
包含工具、故障注入与重启。样本和负载不同，不能据此宣称 provider 或 Runtime 性能提升。

## 保留的失败

不是所有尝试都通过，最终批次不能覆盖此前失败：

- [jobs-intermediate.json](../artifacts/2026-10-20-background-results/jobs-intermediate.json)：17/18，失败结果读出有一次模型返回文本工具标签。
- [core-handoff.json](../artifacts/2026-10-20-background-results/core-handoff.json)：27/28，恢复后有一次同类异常输出。
- [readout-failures.json](../artifacts/2026-10-20-background-results/readout-failures.json) 保留实际回复，标签没有被执行。随后加强结果材料提示，明确直接报告已有 status/result、只使用真实工具菜单；最终读出补测 10/10。样本较小，不代表已经消除模型异常输出。
- [scheduler-initial.json](../artifacts/2026-10-20-background-results/scheduler-initial.json)：12/13，旧验证器等待三条渠道回复，但前两条已按来源 epoch 变为 superseded。改为明确撤销旧请求、仅执行最后输入，检查两条 superseded、一次投递及零次旧写入，最终 13/13。
- [workspace-tool-choice.json](../artifacts/2026-10-20-background-results/workspace-tool-choice.json)：5/6；模型通过 Shell 正确读取了重建会话的文件，但旧断言隐含要求 read_file。任务改为明确要求 read_file 的相对参数，同时加强参数断言，最终 6/6；这次失败不代表目录恢复出错。
- [unit-registration-failure.txt](../artifacts/2026-10-20-background-results/unit-registration-failure.txt) 暴露局部重启时旧钩子与工具贡献未撤销。修复注册 disposer 和原生 Effect 贡献后，[62 项定向回归](../artifacts/2026-10-20-background-results/unit-activation-targeted.txt)及在线 Jobs 替换通过。
- 同时启动多组模型场景和测试曾触发 NFS/I/O 等待及 MCP/CLI 启动超时：[负载下的测试记录](../artifacts/2026-10-20-background-results/unit-io-failure.txt)与同目录 `*-io-interrupted.json` 保留。停止该批次后采用串行在线验证，再单独执行全量测试和压力验证；没有修改生产启动超时。

全量回归中还暴露了旧测试的进程启动计时不稳定：[原始失败日志](../artifacts/2026-10-20-background-results/unit-readiness-fixture-failure.txt)。
即时退出 fixture 在 POSIX 下使用本机 Shell 退出，避免 NFS 上 Python 解释器/site 启动混入短暂存活窗口；Windows 保留同一解释器的 `-S`。没有延长生产等待或放宽回滚断言。

## 离线回归与来源

全量 **712 项测试、4 项子测试**通过，22 条已有 Telegram 弃用警告；
[全量日志](../artifacts/2026-10-20-background-results/unit-final.txt)和
[62 项所有者定向回归](../artifacts/2026-10-20-background-results/unit-activation-targeted.txt)均保留。

千轮压力验证 **1,000/1,000**：64 个 Session、5,000 次工具调用、1,000 次实际计数写入、
77 次受控重试，未知执行结果为 0；重启后恢复 2,000 条原消息。
单次吞吐 **54.16 轮/s**，见[pressure.json](../artifacts/2026-10-20-background-results/pressure.json)。
这不是模型质量评测，也不是配对性能实验；此前架构重构的吞吐回退尚不能宣称已经恢复。

构建 wheel/sdist、CLI、静态检查、33 个固定文件与 Yoyo 迁移不可变检查通过。
实验在提交前的工作树运行，JSON 中的 base commit 不代表已提交源码身份；
[source.json](../artifacts/2026-10-20-background-results/source.json)记录 342 个 Runtime Python 文件的摘要。
最终报告中已记录的源码摘要均与本轮代码一致。仓库保存最终逐项结果和失败批次；中间通过的重复批次留在隔离工作目录。JSON 保留原样，pytest 日志仅去除行尾空白。

## 复现

使用同一项目 `.venv` / Python 3.14.6，workspace 和 output 必须是新路径：

```bash
uv run --frozen python -B -m eval.scenarios.core \
  --key-file /path/outside/repo/step-plan.key \
  --workspace /tmp/kira-core-new --output /tmp/kira-core-new.json \
  --repeats 2 --concurrency 3

uv run --frozen python -B -m eval.scenarios.core \
  --key-file /path/outside/repo/step-plan.key \
  --workspace /tmp/kira-readout-new --output /tmp/kira-readout-new.json \
  --categories job_failed job_resume --repeats 5 --concurrency 2

uv run --frozen python -B -m eval.benchmarks.runtime \
  --workspace /tmp/kira-pressure-new --output /tmp/kira-pressure-new.json \
  --turns 1000 --sessions 64
```

这些是合成功能合同，不能当作外部任务榜单、完整记忆质量评测或所有上游功能完成证明。
下一轮架构优先级仍是统一 Message 与投影、同轮 steer/撤回和原生执行宿主；Jobs 的完整 Shell/进程
生产者与工作台任务面板也仍在功能范围内。
