# 2026-10-19：Runtime 与记忆复测

所有真实生成均为 Step5 Token Plan，原生 medium、8,192 输出上限。仅提交公开/合成输入产生的
结果、源 ID、版本与摘要；完整数据、向量、模型权重、运行数据库和密钥留在仓库外。

- `scheduler-r1.json`、`scheduler-r2.json`：48 和 52 个生产 Runtime 场景、模型尝试、usage 和延迟。
- `core.json`、`drift.json`、`wake.json`、`memory.json`：23 个整链/工具场景；memory 使用固定向量。
- `memory-repeat.jsonl`、`memory-oracle-repeat.jsonl`：原固定 36 题四组与 oracle 的原生配置重复。
- `memory-baselines.jsonl`、`baseline-coverage.json`：同 36 题的 BM25、RRF、近期窗口与精确原文覆盖。
- `cost-*`：公开合成轨迹、各阶段成本、故障和恢复，只有清单、结果与 receipt，没有运行数据库。
- `upstream-cost-272`、`upstream-parity.json`：实际上游脚本与 Kirakira 的输入、完整学习状态和九个查询摘要。
- `math-properties`：固定上游数学性质，原始容器差异失败与仅适配容器后的数值断言结果。
- `holdout-selection.json`：新问题保留集，在回答前冻结，不复用先前 36 个 question ID。
- `experiment-notes.json`：适配期失败与修正原因。

指标、限制和复现命令见[Runtime 报告](../../../docs/operations/evaluations/2026-10-19-scheduler.md)与
[记忆报告](../../../docs/operations/evaluations/2026-10-19-memory-followup.md)。
