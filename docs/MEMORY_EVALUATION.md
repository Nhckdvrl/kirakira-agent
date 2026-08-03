# 记忆评测

> 当前 Reference 基线：`af49848937c4b62abb2f40a7d91b5f90ea71be6d`。本页的 A/B 是**零成本
> 确定性 Gate**，不能据此声称某个引擎回答质量更高。另有真实 DeepSeek + embedding 的 Akasha
> smoke test，但它同样不是 LongMemEval/PersonaMem 质量评分。

## 当前可运行的免费 Gate

```bash
eval_dir="/tmp/kirakira-memory-ab-$(date +%Y%m%d-%H%M%S)"
uv run python -m eval.longmemeval.offline_ab --workspace "$eval_dir"
```

它固定跑 Reference LongMemEval 支持的三类 smoke：

- `single-session-user`
- `single-session-preference`
- `knowledge-update`

每题分别建立 `default/<question-id>` 与 `akasha/<question-id>` 两个隔离 workspace。命令不读取
`config.toml`、正式 session 或正式记忆，也不读取 API key；LLM 与 embedding 分别替换为固定本地
provider 和 hashing vectorizer。任何 HTTP 调用都会立即失败。

报告写入 `<workspace>/report.json`，包含：

- Recall@10 与 MRR；
- 每个命中的 rank、score、source ref 与证据 rank；
- `sessions.db` 和记忆库的 `PRAGMA integrity_check`；
- Memory2 的 `memory_items` 数量；
- Akasha 的 message embeddings、nodes、edges、activation events 数量；
- 固定 LLM/embedding 调用计数，并明确记录 `paid_api_calls = 0`。

## 摄入为什么分两条

Memory2 按 Reference 的结构化记忆路径接收 `ConsolidationCommitted`，写入 `coremem.db`。免费 Gate
用确定性 history entry 代替付费 consolidation 模型输出，只验证事件、写入、检索和证据链。

Akasha 不消费 consolidation。它按真实状态机逐轮执行：

```text
当前 user message 查询旧图
  → user/assistant 写入 sessions.db
  → TurnCommitted
  → message embedding / turn node / activation edge
```

因此不能把 Reference LongMemEval 的 `memory="default"` 简单改成 `akasha`；原 runner 直接写历史并
手工 consolidation，不产生 Akasha 需要的逐轮激活和 `TurnCommitted`，会得到近似空图。

## 用官方 LongMemEval JSON 做免费结构验证

下载数据后可以传入任意兼容 JSON；默认限制前三题：

```bash
uv run python -m eval.longmemeval.offline_ab \
  --workspace /tmp/kirakira-memory-ab-data \
  --data /path/to/longmemeval_s_cleaned.json \
  --limit 3
```

这仍然不是官方最终 QA：它只证明真实引擎能摄入、持久化、检索并回源。官方 `judge/F1/EM` 需要真实
AgentLoop 和模型调用，后续有预算时另跑，结果必须与免费 Gate 分栏报告。

## 测试

```bash
uv run pytest -q tests/test_memory_eval_offline.py
uv run pytest -q tests/test_akasha.py tests/test_memory_m1.py tests/test_retrieval.py
uv run kirakira-verify-online
```

相关实现：

- `eval/longmemeval/dataset.py`：Reference LongMemEval 数据合同。
- `eval/longmemeval/metrics.py`：确定性 EM/F1/MRR 基础指标。
- `eval/longmemeval/offline_ab.py`：隔离双引擎 runner。
- `tests/test_memory_eval_offline.py`：网络零调用、双引擎落库、证据命中与 workspace 隔离。

## 质量评测仍未完成

- 完整 LongMemEval 的真实 AgentLoop 最终 QA 与 DeepSeek Judge（基础 AgentLoop/Akasha 在线 smoke 已通过）。
- LongMemEval `answer_session_ids/has_answer` 的完整官方 retrieval 指标适配。
- Memory2 knowledge-update 的 supersede/replacement 独立状态 oracle。
- Akasha live/replay 全量图 parity。
- PersonaMem 当前官方类型名与 Reference runner 的 schema 漂移兼容。

以上事项完成前，免费 Gate 只能作为链路与持久化门禁，不能代替模型质量评测。
