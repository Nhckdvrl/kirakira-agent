# 2026-10-19：Runtime 与记忆复测

所有真实生成均为 Step5 Token Plan，原生 medium；reader/judge 上限 8,192，最终 Mem0 写入上限 32,768。
仅提交公开/合成输入产生的
结果、源 ID、版本与摘要；完整数据、向量、模型权重、运行数据库和密钥留在仓库外。

- `scheduler-r1.json`、`scheduler-r2.json`：48 和 52 个生产 Runtime 场景、模型尝试、usage 和延迟。
- `core.json`、`drift.json`、`wake.json`、`memory.json`：23 个整链/工具场景；memory 使用固定向量。
- `memory-repeat.jsonl`、`memory-oracle-repeat.jsonl`：原固定 36 题四组与 oracle 的原生配置重复。
- `memory-baselines.jsonl`、`baseline-coverage.json`：同 36 题的 BM25、RRF、近期窗口与精确原文覆盖。
- `cost-*`：公开合成轨迹、各阶段成本、故障和恢复，只有清单、结果与 receipt，没有运行数据库。
- `upstream-cost-272`、`upstream-parity.json`：实际上游脚本与 Kirakira 的输入、完整学习状态和九个查询摘要。
- `upstream-head-check.json`：再次核对当天 `21b38f0`，十个算法文件、recalls 与公开 cost 协议共 13 个文件未变。
- `math-properties`：固定上游数学性质，原始容器差异失败与仅适配容器后的数值断言结果。
- `matvec-shapes.json`：固定 672 种矩阵形状与完整 NumPy 计算逐位相等；平台与形状边界随报告保存。
- `holdout-selection.json`、`holdout-results*`、`holdout-oracle*`、`holdout-coverage.json`：24 道新问题；
  此时 `akasha2` 是旧 graph-only 读出；embedding/检索/控制各有独立 manifest。
- `readout-dual24*`、`readout-diagnostic24-coverage.json`：同 24 个图恢复后的双通道诊断，学习状态不变；不算新题。
- `readout-selection.json`、`readout-fresh12*`、`readout-oracle12*`、`readout-fresh-coverage.json`：
  排除先前 60 题后的新 12 题。此处 `akasha2` 是当前双通道，`akasha2_graph_only` 是 eval 消融。
- `memory-dual-lane.json`：修复后真实 Step5 记忆工具及恢复 6/6，hash 向量只证明功能。
- `mem0-selection.json`：24 题中预定每类前两题，共 12 题；与 fresh12 是不同题组。
- `mem0-ingestion.jsonl`、`mem0-ingestion.manifest.json`：最终空库重跑的 12/12 成功写入，74 次成功模型请求；
  每题含事实、来源 bookkeeping、逐次用量与计时，不含密钥、提取阶段完整 prompt 或运行数据库。
- `mem0-answers.jsonl`、`mem0-answers.summary.json`：正式同题 reader/judge，9/12、零运行错误。
- `mem0-assistant-diagnostic.json`：判分后只读检查两道助手历史题的关键词事实，没有重写或重评分。
- `mem0-initial*`：每会话/8,192 上限的中止预检，保留截断与部分用量，不给质量分数。
- `mem0-id-marker-preflight*`：发现来源 ID 可能提示答案后中止的预检，没有进入正式 QA 计分。
- `experiment-notes.json`：适配期失败与修正原因。
- `adapter-timings.json`：24 题和新 12 题的逐题学习时间与五次只读 query 样本，embedding 已缓存。
- `memory-comparison-summary.json`：各题组的分类准确率、弃答、错误、用量、覆盖/MRR 与计时汇总。
- `pytest-final.txt`：同一 Python 3.14.6 环境的最终 694 项测试与 4 个 subtests。
- `source-provenance.json`：24 题诊断后 selector 的模块说明补 MIT 归属，保留可复算的旧/新哈希；
  去除模块 docstring 后 AST 完全相同，新 12 题与最终代码摘要一致。
- `verification-manifest.json`：同一环境的包版本、源文件/原始结果/最终构建摘要与最后验证口径。

固定题 36 + holdout24 + fresh12 共 72 个不同 question ID；重复与 Mem0 子集不增加独立样本。
旧结果不改写为新 adapter 成绩；中止预检不挑成功案例补进最终批次。

指标、限制和复现命令见[Runtime 报告](../../../docs/operations/evaluations/2026-10-19-scheduler.md)与
[记忆报告](../../../docs/operations/evaluations/2026-10-19-memory-followup.md)。
