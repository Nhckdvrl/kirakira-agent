# 记忆评测

合同回归与回答质量分别验证。确定性 hash 向量只用于消费、重放、幂等与持久化测试，不能支持记忆质量结论。
公开 QA、逐题文件与限制见[公开机制与系统对照](./evaluations/2026-10-19-memory-followup.md)，
原文证据、丢图重建和专项合同见[事件记忆实验](./evaluations/2026-10-19-episodic.md)。
早期 graph-only 读出与配置修正保留在[历史报告](./evaluations/2026-10-19-memory.md)，不作当前 adapter 成绩。

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
免费 CPU embedding 与 Agent 共用项目 `.venv` / Python 3.14.6；FastEmbed 仅属于可选 eval 依赖组：

```bash
uv sync --frozen --group dev --group eval

uv run --frozen --group eval python -m eval.longmemeval.embed_local \
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


## 公开机制、成本与检索基线

当前 Akashic 的 LongMemEval/PersonaMem 入口已退役；其公开可复现重点是
[scripts/akasha_cost_scenario.py](https://github.com/kachofugetsu09/akashic-agent/blob/d85b00c233686b68b082052b1b20123905aeab13/scripts/akasha_cost_scenario.py)
和[机制/成本报告](https://github.com/kachofugetsu09/akashic-agent/blob/d85b00c233686b68b082052b1b20123905aeab13/docs/design/akasha-memory-cost.md)。
Kirakira 同时保留公开任务质量和机制验证，不把上游私人语料成绩当成本项目成绩。

```bash
PYTHONHASHSEED=0 uv run --frozen python -m eval.memory_cost --output /tmp/cost-seed0
PYTHONHASHSEED=42 uv run --frozen python -m eval.memory_cost --output /tmp/cost-seed42
uv run --frozen python -m scripts.check_akasha2_properties --output /tmp/upstream-properties
uv run --frozen python -m scripts.check_akasha2_matvec --output /tmp/matvec-shapes.json

uv run --frozen python -m eval.longmemeval.baselines \
  --dataset /absolute/cache/longmemeval_s_cleaned.json \
  --embeddings /absolute/cache/bge-36.npz \
  --graph-retrieval /tmp/kirakira-retrieval.jsonl \
  --output /tmp/kirakira-baselines.jsonl
```

`memory_cost` 使用与上游相同的 272 条合成经历协议：跨会话、长间隔、remember/forget、
部分缺向量、9 个查询、连续写入与完整 writer 的全表比较、16 次重启，以及 file fsync/replace/
directory fsync 故障。报告同时保存完整状态与召回摘要、学习/查询/发布/恢复 p50/p95、RSS 和数据库大小。
这是纯 MemoryCycle 与快照开销，不包含模型、远程 embedding 或完整宿主；合成向量不证明语义质量。

数学性质检查固定独立引擎提交，运行其数值断言；仅改导入路径和三个 list/array 读法，保留全部
数值不等式。独立引擎的 CLI/schema 专属测试明确排除，以 Kirakira 的两种哈希种子实验另验确定性。

基线增加 FTS5 BM25、Dense/BM25 RRF（k=60）和近期原文窗口，均最多 10 对/24,000 字符。
这些是可复现的检索控制，不是 Mem0/Zep 的实现或产品成绩。源 ID 回源校验后才计算标注消息对覆盖率，
同时记录回答正确率、完整覆盖、MRR、生成用量和延迟。

新增题集须先冻结 selection JSON，以 `--selection` 同时传给 embedding 与 prepare_retrieval；
原来的固定 36 题默认协议保持。题目保留集与历史语料完全独立是不同要求，报告必须标明分割单位。

## 双通道与旧读出消融

当前 Akasha2 的精确 dense 与 completion 共用 `MemoryQuery.limit` 总预算；旧结果中的 `akasha2`
可能指退役 graph-only adapter，必须以当轮源码摘要和报告说明区分。生产引擎不提供旧模式开关。
对新题只准备 v2，并在同一学习图加入旧读出控制：

```bash
uv run --frozen python -m eval.longmemeval.prepare_retrieval \
  --dataset /absolute/cache/longmemeval_s_cleaned.json \
  --embeddings /absolute/cache/bge-new.npz \
  --selection /absolute/cache/frozen-selection.json \
  --workspace /tmp/readout-new-run --output /tmp/readout-new.jsonl \
  --akasha2-only --graph-only-control
```

命令保存五次只读查询计时和完整学习状态摘要；不包含 embedding 推理，因为向量已缓存。
需在相同旧图上诊断当前读出时，`requery_readout` 读取旧检索列表及其 `cases` 目录：

```bash
uv run --frozen python -m eval.longmemeval.requery_readout \
  --dataset /absolute/cache/longmemeval_s_cleaned.json \
  --embeddings /absolute/cache/bge-old.npz --retrieval /tmp/old-graph.jsonl \
  --cases /tmp/old-run/cases --output /tmp/readout-diagnostic.jsonl
```

它要求查询、恢复与关闭前后学习状态相同；这类已观察题目仅是诊断，不计为新的保留集。


## Mem0 OSS 实际对照

在同一个项目环境安装可选对比组：`uv sync --frozen --group dev --group eval --group comparison`。
`compare_mem0_step_plan` 固定本地 Qdrant 与 BGE、关闭 telemetry，真实 Mem0 执行原生 additive 提取、
去重、存储与搜索，
只有模型 HTTP 传输改接受校验的 Step Token Plan。没有访问默认 OpenAI 计费端点。

```bash
uv run --frozen --group eval --group comparison python -m scripts.compare_mem0_step_plan \
  --dataset /absolute/cache/longmemeval_s_cleaned.json \
  --selection eval/results/2026-10-19-scheduler/mem0-selection.json \
  --key-file /absolute/private/step-plan.key --cache /absolute/cache/models \
  --workspace /tmp/new-mem0-run --output /tmp/new-mem0-ingestion.jsonl \
  --workers 4 --sessions-per-add 8 --ingest-max-tokens 32768

uv run --frozen --group eval --group comparison python -m eval.longmemeval.live_eval \
  --retrieval /tmp/new-mem0-ingestion.retrieval.jsonl \
  --output /tmp/new-mem0-answers.jsonl --key-file /absolute/private/step-plan.key \
  --concurrency 4 --max-tokens 8192 --reasoning-effort medium
```

以生成的 `.retrieval.jsonl` 再调用同一个 live_eval reader/judge。Mem0 为 OSS 基础安装，未安装
可选 spaCy NLP extra；不代表 hosted Platform 或所有部署组合。固定每八个完整来源会话一次
add(infer=True)，末批保留剩余会话，按给定日期排序。原始消息角色和正文全量保留，图实验则仅学习
完整 user/assistant 对；这项输入投影差异也须报告。

问题与答案不进入提取；原始日期作为上下文和 metadata。数据集来源 ID 可能编码答案位置，
所以提取 prompt 只含原文与日期；模型传输前另检查题目/来源 ID 没有泄漏。ID 只留在 bookkeeping。
Mem0 的原始系统 prompt 保留，只有受校验的 HTTP 传输和 BGE query 前缀做适配。
返回最多 10 条提取事实，而图检索返回原文对，
这属于系统间差异，不能把 Mem0 事实的 pair recall 当零分。只在预先固定的同题子集比较回答和用量，
并报告初始化、批量学习和检索开销。Mem0 搜索计时含本地 query embedding，图搜索计时使用缓存，
不能直接计算系统速度比。提取上限 32,768 与 reader/judge 8,192 分开记录。
失败输入与失败模型调用必须保留；失败写入不进入 reader 猜答案，而是按 ingestion 错误计入分母。
未完成、截断和发现 fixture ID 泄漏后中止的预检单独归档，不能挑其中成功案例并入最终成绩。

## 事件记忆专项合同

`episodic_fixture` 先固定合成数据、seed、全部题目和严格答案；它不是 LongMemEval 的新版本。
六类各四题，包含偏好和弃答，不按某个系统得分挑选。所有模型调用前保留生成的 `protocol.json`。

```bash
uv run --frozen --group eval --group comparison python -m eval.episodic_fixture --output /tmp/episodic-new
uv run --frozen --group eval --group comparison python -m eval.longmemeval.embed_local \
  --dataset /tmp/episodic-new/dataset.json --selection /tmp/episodic-new/selection.json \
  --cache /absolute/cache/models --output /tmp/episodic-new/vectors.npz
uv run --frozen python -m eval.longmemeval.prepare_retrieval \
  --dataset /tmp/episodic-new/dataset.json --selection /tmp/episodic-new/selection.json \
  --embeddings /tmp/episodic-new/vectors.npz --workspace /tmp/episodic-new/cases \
  --output /tmp/episodic-new/graph.jsonl --akasha2-only
uv run --frozen python -m eval.longmemeval.baselines \
  --dataset /tmp/episodic-new/dataset.json --embeddings /tmp/episodic-new/vectors.npz \
  --graph-retrieval /tmp/episodic-new/graph.jsonl --output /tmp/episodic-new/baselines.jsonl
```

baseline 文件只含 BM25/RRF/recency；必须同时传入 graph 文件才包含 no_memory/dense/akasha2。
按上一节使用同一合成 dataset/selection 运行真实 Mem0。其写入结束后统一评分：

```bash
uv run --frozen python -m eval.episodic_eval \
  --dataset /tmp/episodic-new/dataset.json \
  --retrieval /tmp/episodic-new/graph.jsonl /tmp/episodic-new/baselines.jsonl /tmp/mem0-new.retrieval.jsonl \
  --output /tmp/episodic-new/answers.jsonl --key-file /absolute/private/step-plan.key
```

评分前保存输入、reader 源码及模型参数身份；续跑不允许更换身份。无模型 judge，按 JSON 精确值判定，
格式错误留在分母。偏好短语、标识、关系与弃答分开报告；简单合同题通过不能证明普遍检索优势。

## 原文展开与源库独立重建

新 Akasha2 在线学习会保存按完整空间身份绑定的原始 float32 向量，再提交学习。离线重建自动识别
新缓存；旧 model-only 缓存在报告中单独标记。缺失、损坏或多个不明确空间继续严格失败，不联网补算。
已有旧历史不会自动回填，测试不得删除真实派生库来演练。

`verify_episodic_recovery` 接收冻结和最终两版检索文件、最终测试 cases 与固定向量：
验证 reader 输入不变、完整 source_ref 原文展开、重启记录相等，再只用源库从空图重建。
结果同时比较学习表与完整消费 JSON，并拦截网络连接：

```bash
uv run --frozen python -m scripts.verify_episodic_recovery \
  --original-retrieval /tmp/episodic-new/graph-frozen.jsonl \
  --final-retrieval /tmp/episodic-new/graph-final.jsonl --cases /tmp/episodic-new/final-cases \
  --embeddings /tmp/episodic-new/vectors.npz --output /tmp/episodic-new/recovery.json
```

旧/新源库 IO 对照使用 `scripts.benchmark_memory_source`，从有完整历史的本仓库加载固定旧版本。
同图比较 source connections、恢复/query 样本、原文/顺序及分数差；报告包含先后测量和缓存限制。
