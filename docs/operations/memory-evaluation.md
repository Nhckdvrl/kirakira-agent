# 记忆评测

合同回归与回答质量分别验证。确定性 hash 向量只用于消费、重放、幂等与持久化测试，不能支持记忆质量结论。
本轮实测结果、逐题文件与限制见 [2026-10-19 实验报告](./evaluations/2026-10-19-memory.md)。

## 三层证据

1. 离线合同：原始消息消费、发布位置、查询不学习、重复通知、故障回滚、快照恢复与固定向量重建。
2. 在线功能：真实 Step5 驱动工具链、当前生成恢复、完整对话入口、Wake 与 Drift；测试渠道是本地回执，不向真实用户发送消息。
3. 公开语料质量：固定 LongMemEval 样本，比较无记忆、纯向量、Akasha v1、Akasha2；另外记录标签辅助的 oracle 诊断。

每层保留自己的失败。通过前两层不等于第三层有提升；oracle 使用标注，不能当作有效检索成绩。

## 运行离线合同与固定向量重建

```bash
uv sync --frozen --group dev
uv run pytest -q
uv run python -m scripts.replay_akasha --help
uv run python -m scripts.replay_akasha2 --help
```

重建命令读取原始 SQLite 的一致备份，只使用内容 hash、模型身份和维度匹配的固定向量。
缺失、损坏或混杂的向量会失败，不访问网络，不覆盖来源，不自动激活候选。
同时间戳的 v1 消费和重建遵循持久 publication 顺序，不按文本形式的序号排序。
切换与恢复要求见[记忆手册](../handbook/memory.md)。

## 在线功能验证

Step5 使用官方 Token Plan 专用端点 `https://api.stepfun.com/step_plan/v1`，实际模型 ID 为
`step-5-preview`。验证器强制该路由，不回退到现金 API；原始密钥只从外部文件读入内存。Step5 使用 reasoning_effort（low/medium/high），
不能用 thinking=false 声称关闭推理。
模型、工具、记忆客户端不能复用真实 workspace 的私有配置。

```bash
uv run python -m scripts.verify_step_plan \
  --key-file /absolute/private/step-plan.key \
  --workspace /tmp/kirakira-core-new-run \
  --output /tmp/kirakira-core-result.json

uv run python -m scripts.verify_drift_step_plan \
  --key-file /absolute/private/step-plan.key \
  --workspace /tmp/kirakira-drift-new-run \
  --output /tmp/kirakira-drift-result.json

uv run python -m scripts.verify_wake_step_plan \
  --key-file /absolute/private/step-plan.key \
  --workspace /tmp/kirakira-wake-new-run \
  --output /tmp/kirakira-wake-result.json
```

每次使用新的 workspace。Wake 区分“本地已接收”的 receipt ACK 与“实际选中”的兴趣 feedback；
ACK 身份集合不依赖数据库返回顺序。重复 tick 与重启不应重复评分或发送已处理到达。
Drift 的静默、实际投递、来源失效与 paused 分别验证。

## 固定质量协议

- 数据：`xiaowu0162/longmemeval-cleaned` 的 LongMemEval-S，revision
  `98d7416c24c778c2fee6e6f3006e7a073259d48f`，SHA-256
  `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`。
- 选择：按 `sha256(kirakira-20261019:question_id)` 排序，六种 question type 各取 6 题；
  不使用答案或 gold session 选择样本。共 36 题，其中 3 道自然抽中的 abstention。
- 向量：本地 `BAAI/bge-small-en-v1.5`，FastEmbed/ONNX CPU，384 维、512 token 截断；
  query 使用模型对应的检索前缀。缓存记录所有下载文件摘要，不请求托管 embedding API。
- 输入：每题隔离来源和图，按提供的日期排序，消息 ID 从题目/来源/位置确定生成。
  原始消息完整落库；四组比较使用相同的完整 user/assistant 消息对投影，不完整消息数量逐题报告。
- 时间：数据集没有时区，统一解释为 UTC。部分历史日期晚于问题日期；保留所有给定 haystack，
  消费查询时钟取二者最大值，reader 仍按原始问题日期推理。该实验不是生产历史快照查询测试。
- 检索：Dense 取消息对内 user/assistant 相似度的最大值；图引擎使用生产 query 顺序。
  各组都回源到完整原文再给 reader，最多 10 对、24,000 字符；不使用 v1 截断的 assistant 展示卡。
- Reader：Step5，原生 reasoning_effort=medium，max_tokens=8192；只看问题、日期与检索原文，不看答案或 gold ID。
- Judge：官方 LongMemEval 分类判分 prompt，固定来源 revision
  `9e0b455f4ef0e2ab8f2e582289761153549043fc`；judge 也是 Step5，严格 yes/no，错误计入分母。
- Oracle：只展示带 `has_answer` 标注的完整原文对。它有意使用答案位置标注，只诊断 reader/检索瓶颈。

## 准备与执行

先从上述固定 revision 下载 `longmemeval_s_cleaned.json`，核对 SHA-256。数据和缓存留在仓库外。
为免费 CPU embedding 单独创建环境，不把 FastEmbed 加入生产依赖：

```bash
uv venv /tmp/kirakira-embedding-env
uv pip install --python /tmp/kirakira-embedding-env/bin/python fastembed

/tmp/kirakira-embedding-env/bin/python -m eval.longmemeval.embed_local \
  --dataset /absolute/cache/longmemeval_s_cleaned.json \
  --output /absolute/cache/bge-36.npz \
  --cache /absolute/cache/models --per-type 6

uv run python -m eval.longmemeval.prepare_retrieval \
  --dataset /absolute/cache/longmemeval_s_cleaned.json \
  --embeddings /absolute/cache/bge-36.npz \
  --workspace /tmp/kirakira-lme-new-run \
  --output /tmp/kirakira-retrieval.jsonl --per-type 6

uv run python -m eval.longmemeval.live_eval \
  --retrieval /tmp/kirakira-retrieval.jsonl \
  --output /tmp/kirakira-answers.jsonl \
  --key-file /absolute/private/step-plan.key --concurrency 4
```

独立样本可通过 `prepare_retrieval --shards N --shard-index I` 分进程运行，每个 shard 使用独立输出，
共享一个 workspace 根但不共享题目。用 `merge_retrieval --inputs ... --output ... --expected-cases 36`
合并；它会检查各 shard 的数据、代码和协议一致。reader 可添加 `--follow --expected-cases 36` 边准备边运行。
模型请求内部按 core 的有界重试策略恢复；已落盘的逐题结果续跑时不会被覆盖，也不把失败重试到成功后隐藏。

Oracle 单独运行：

```bash
uv run python -m scripts.prepare_memory_oracle \
  --dataset /absolute/cache/longmemeval_s_cleaned.json --output /tmp/kirakira-oracle.jsonl
uv run python -m eval.longmemeval.live_eval \
  --retrieval /tmp/kirakira-oracle.jsonl --output /tmp/kirakira-oracle-answers.jsonl \
  --key-file /absolute/private/step-plan.key --concurrency 4
```

将 embedding 和 retrieval 的 `--per-type` 一起改为至少 133 可覆盖完整 500 题；这是新的实验，
不能把当前 36 题的分层比例直接称为官方总体成绩。运行前固定协议，运行后保存全部逐题输出与异常。

## 覆盖指标

session recall 只说明某个相关 session 被命中过，不能证明答案所在的消息对被取到。
结果另外保存事后匹配的标注消息对索引，用于复算完整证据覆盖。没有可用标注完整消息对的题
不进入这个覆盖指标，但仍保留在回答正确率分母中；每次明确报告该数量。

## 必须报告

数据/模型/代码摘要、选择范围、字符预算、每组正确数、分类结果、弃答、证据 session recall、
错误与截断、reader/judge usage，以及 oracle 对错误的诊断。成功响应 usage 不等于包含所有失败尝试的计费账单。
同模型 judge 存在偏差；小样本、单轮和单 embedding 不能支持显著性或整体领先结论。首轮曾发送未获文档支持的 thinking=false、上限 4096；
该轮原样保留并标明实际推理强度未指定，统一原生配置的复跑另存为独立轮次。
