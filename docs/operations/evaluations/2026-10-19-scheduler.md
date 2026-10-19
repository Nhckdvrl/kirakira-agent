# Scheduler、会话主链与真实 Step5 验证

日期：2026-10-19。Python 3.14.6，项目唯一 `.venv`。上游固定 `d85b00c`。

## 实现

定时任务进入创建会话，提供 notify/run 与 none/session 历史范围。持久 fire 在模型开始前记录
running，投递前记录 delivering 和真实原文 ID，等待本地回调成功后 completed。pending fire 可
恢复；运行中崩溃、未知工具结果和不明确投递结果保留 needs_reconciliation，不自动重放。

渠道删除重复锁，共享 Pipeline admission。任务原文和回复有明确 source，不学习为用户记忆、
不重置用户活跃或空闲时间；大量任务也不能挤掉近期人类上下文。旧文件通过独立 Yoyo 迁移，
先完整校验再保存精确原始字节备份。初始化向导使用当前格式。

删除没有生产调用的同步 Agent、旧手工压缩入口、旧 MemoryRuntime 包装和未接线的图快照。
根 sessions.db 停止跟踪，本地原文件保留。测试默认拒绝打开仓库内的真实数据库。
边界见 [ADR0011](../../decisions/0011-source-session-scheduler.md)。

## 真实模型实验

全部调用 `https://api.stepfun.com/step_plan/v1`，模型 `step-5-preview`，原生
reasoning_effort=medium、输出上限 8,192；没有使用现金账户接口。密钥只从仓库外文件读入内存。
每个场景使用全新 workspace，工具限定为本地文件、Scheduler 与明确的故障 fixture，投递为本地回执。

| 批次 | 场景 | 观察到的模型请求 | 成功响应 | 已返回 usage 的 token |
| --- | ---: | ---: | ---: | ---: |
| 首轮 Scheduler | 48/48 | 110 | 110 | 170,658 |
| 统一 admission 后复跑 | 52/52 | 238 | 144 | 236,200 |
| Core/Wake/Drift/记忆工具回归 | 23/23 | 见各逐项报告 | — | 未统一汇总 |

第二轮包含 94 次失败请求尝试，均由现有有界模型重试恢复，最终场景没有删题或失败。采集器只保存
异常类型，不能据此认定全部来自某一种 HTTP 状态。两轮同时运行的其他实验负载不同，不能用
延迟变化声称实现变快或变慢。token 是已收到响应的 usage，不是账户账单或失败请求的完整用量。

第二轮场景延迟 p50/p95 为 8.03/47.41 秒；模型尝试延迟 p50/p95 为 1.49/5.23 秒。
通知、开始前取消和未知运行态的启动核对场景预期零次模型调用，不把它们计为模型回答成功样本。

13 类场景各重复四次，包含原样通知、原会话历史、任务内工具、定时器真实触发、模型创建提醒、
重复触发、队列恢复、投递失败重启、未知工具取消、直接并发、渠道并发、来源回源和执行前取消。
最终重复触发/重启没有额外投递，未知工具 fixture 的效果只发生一次；排队取消没有幻影输入；
run 的原消息 ID 与渠道投递引用一致。来源检查确认任务对不可学习，人类原文仍可回源。

另行回归 Core/Wake 12、Drift 4、Wake 整链 1、记忆工具 6，共 23 个真实模型场景。
记忆工具使用固定向量，只验证工具调用、原文解析与重启链路。真实语义质量另见
[记忆实验](2026-10-19-memory-followup.md)。

## 复现与原始证据

```bash
uv sync --frozen --group dev --group eval
uv run --frozen python -m eval.scenarios.scheduler \
  --key-file /absolute/private/step-plan.key \
  --workspace /tmp/new-isolated-scheduler-run \
  --output /tmp/new-scheduler-results.json --repeats 4 --concurrency 4
```

验证器拒绝复用结果路径，失败行增量保存。完整软件与结果摘要见
[原始结果目录](../../../eval/results/2026-10-19-scheduler/README.md)。

本轮没有实现工具边界让出后恢复同一个持久程序，没有跨 SessionDB/执行日志/任务文件事务，
也不把成功回调当作外部平台 exactly-once 或用户已读。未知结果仍需核对；这部分不能宣称完全对齐上游 Ledger。
