# 评测与场景验证

维护和恢复命令在 [`scripts/`](../scripts/README.md)。这里保存可复用的验证程序，以及有明确来源的历史结果。

| 目录 / 模块 | 用途 |
| --- | --- |
| `eval.scenarios` | Core、Scheduler、Workspace、Wake、Drift、记忆和恢复场景 |
| `eval.benchmarks` | 主链 micro、耐久压力、原文读取、Akasha2 数学/矩阵检查 |
| `eval.longmemeval` | 数据准备、固定样本、检索、reader/judge、oracle 和 Mem0 对照 |
| `eval.episodic_eval` / `eval.episodic_fixture` | 合成事件记忆合同及 fixture |
| `eval.memory_cost` | 记忆成本与状态轨迹 |
| `results/` | 带日期的历史实验结果；保留原始来源路径和哈希 |

离线示例：

```bash
uv run --frozen python -m eval.benchmarks.core --repeats 30 --output /tmp/core-micro-new.json
uv run --frozen python -m eval.benchmarks.runtime \
  --workspace /tmp/core-pressure-new --output /tmp/core-pressure-new.json \
  --turns 1000 --sessions 64
```

真实 Step 场景使用 `python -m eval.scenarios.core --help` 等模块入口。
Core 默认覆盖后台结果；可用 `--categories job_busy job_idle job_restart background_spawn job_resume job_failed jobs_reload`
单独复测领取、唤醒、恢复、真实子代理和失败回传，`--repeats` 控制重复次数。
凭据只从仓库外 `--key-file` 读取，Token Plan 路由与模型要求由验证器固定；不会默认在 CI 调用。
可选 CPU embedding 和对照依赖通过同一 `.venv` 的 `eval` / `comparison` 依赖组安装。

当前结果见[验证入口](../docs/operations/verification.md)。报告在 `docs/operations/evaluations/`，
历史逐题结果在这里的 `results/`，近期运行证据在 `docs/operations/artifacts/`。
脚本目录调整后，历史 JSON 内的旧脚本路径和摘要仍描述当时运行，不改写这些事实。
