# 评测

评测使用项目同一个 `.venv` / Python 3.14.6。工作区、数据集、模型权重和临时结果放仓库外的本机磁盘；正式结果统一进入 `docs/operations/`。

```sh
uv sync --frozen --group dev --group eval --extra providers
```

## 模型条件

真实模型评测接受 `--model-config /private/path/model.json` 和 `--key-file /private/path/model.key`。配置文件不含密钥：

```json
{
  "driver_id": "openai-compatible",
  "model_id": "evaluation",
  "model": "YOUR_MODEL",
  "endpoint": "YOUR_API_BASE",
  "context_window": 32768,
  "max_output_tokens": 4096
}
```

按实际模型填写能力和预算；支持时可设置 `reasoning_effort`。业务链路不依赖模型 ID 或供应商。省略配置只为兼容既有私有订阅测试命令；新实验应显式提供配置。订阅测试必须填写订阅端点，不得换成现金端点。

报告记录实际模型、驱动、端点与输入条件。同一模型的两个驱动路径不算跨模型验证。日志可能包含首次登录信息，只能留在私有输出目录；不要上传整个工作区或凭据库。

## 功能验证

每次使用不存在或全新的隔离工作区：

```sh
python -m eval.native_runtime \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --workspace /local-disk/eval/core-new --output /local-disk/eval/core.json

python -m eval.native_alignment \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/features-new --output /local-disk/eval/features.json

python -m eval.memory_runtime \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/memory-new --output /local-disk/eval/memory.json

python -m eval.native_web \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --workspace /local-disk/eval/web-new --output /local-disk/eval/web.json

python -m eval.followups \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/followups-new --output /local-disk/eval/followups.json \
  --isolated --long-source

python -m eval.mail_followups \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/mail-new --output /local-disk/eval/mail.json \
  --with-functional

python -m eval.runtime_settings \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/settings-new --output /local-disk/eval/settings.json
```

| 入口 | 验证范围 |
| --- | --- |
| `native_runtime` | 生产插件图中的对话、文件、Shell、MCP、子任务、取消、恢复、附件、调度和投递 |
| `native_alignment` | 在线记忆学习、诊断、事件跳过/投递/去重、实际插件安装、压缩与重启 |
| `memory_runtime` | 真实渠道学习到查询、RPC、Agent 工具、原文展开、项目提醒、显式反馈和重启 |
| `native_web` | 认证后的工作台/聊天 HTTP、import map、项目作用域与实际文件工具 |
| `followups` | 原始对话创建跟进、真实 HTTP 订阅/网页差异、调查分享/跳过、精确投递、反馈、暂停与恢复；可选隔离项目和长原文读取 |
| `mail_followups` | 本地 TLS IMAP 的真实采集、跨会话历史条件与分享/跳过、线程更正、规则修改、外部指令、UID 重建与重启 |
| `runtime_settings` | 实际认证 HTTP 设置、原生配置换代、越界拒绝、清空恢复默认、真实记忆工具预算与图不重学、重启保持 |
| `prompt_cache` | 稳定/变化前缀的受控机制实验；原生邮件调查的实际请求摘要、缓存用量和功能结果；缓存字段未知与零值分开 |
| `idle_reflection` | 空闲规划、跨会话约束、原文依据、原生研究/投递、目标完成后跳过、预算、暂停及交接回执丢失恢复 |
| `tool_runtime` | 文件与网页读取边界、同输入组件测量，以及分页、编辑、目录、并行、Web 恢复、PTY 和大输出的真实 Agent 验收 |

`memory_runtime` 的事件提醒验收固定不含参考答案的近期关注条件；历史决定由其他真实渠道会话学习。除了提醒内容，还检查 Alert 归档的原事件身份、参考对话范围和实际成功的记忆读取回执，避免把此前确认回复已经说出的答案误算为本轮检索能力。

个人伙伴的空闲思考使用独立工作区验证：

```sh
python -m eval.idle_reflection --implicit-goal --fault-cases \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/idle-new --output /local-disk/eval/idle.json
```

`--implicit-goal` 将预算、时间条件放到另一段历史会话，当前只保留未完成话题；没有创建结构化定时任务或提交外部事件。它检查规划实际呈现的历史出处、研究中的记忆工具与真实 HTTP 页面、正确约束和确认投递。`--fault-cases` 在真实生产端写入原生 Drift 提案之后注入一次交接回执丢失，检查重放复用原身份与规划调用，再重启、恢复 Wake 并核对实际结算。已有相同研究结果时，恢复后可以跳过重复通知。两者都是受控功能实验，不能推导日常主动消息的实际有效率或图记忆相对其他检索的净提升。

缓存机制与真实链路分别运行，均使用新的本地工作区：

```sh
python -m eval.prompt_cache --mode probe --rounds 8 \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --workspace /local-disk/eval/cache-probe-new --output /local-disk/eval/cache-probe.json

python -m eval.prompt_cache --mode mail --attention-cases \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/cache-mail-new --output /local-disk/eval/cache-mail.json
```

请求跟踪目前支持 `openai-compatible` 和 `step-plan` 驱动；它观察实际插件代中的传输，不修改生产 Prompt、工具目录、请求路由或流式选择。报告只存请求摘要和大小，不存请求正文或凭据。`mail` 另存 `.functional.json`，保留原有功能验收结果；`--attention-cases` 在原有 8 项之外增加 4 项一般知识、话题、补材料和限时确认的内容判断，分别核对跳过与实际投递。字符前缀只是本地诊断，不是供应商的实际 token 前缀。机制实验可以说明缓存复用条件，不能替代产品优化前后的效果和费用对照。

`eval/fixtures/native_extension` 提供真实托管 HTTP 进程和 stdio MCP 服务；事件投递目标为隔离的本地会话。它们是受控集成验收，不是外部账号、所有设备或所有可选提供方的部署认证。

`followups` 的本机订阅源提供变化事实，不携带参考答案或人工兴趣分。明确订阅通过独立的调查请求接纳；语义兴趣仍如实记录。`--isolated` 验证调查范围与通知位置分离，`--long-source` 明确要求摘录后展开原文，检查工具可用性；它不是自主选择检索策略的质量基准。加 `--webpage` 将前两条变化切换为真实 HTTP 网页的前后版本，仍经过相同调查、记忆、投递与恢复链路。端到端用例不计作公开通知有效率。

邮件实验用六个固定场景，每个场景的邮件相同，在另一个历史对话中分别放置应分享或应跳过的用户条件。每个条件另有移除该历史的观察组，共 24 次调查；通知收件位置独立于项目隔离。`--family-start` / `--family-end` 可拆分场景，`--functional-only` 只跑功能验收。无历史组本来就缺少个人条件，不能把它当作公平的其他记忆系统基线或现实通知准确率。首轮错误保留，功能断言与行动符合率分别报告。

已有首轮出现地域条件误解时，可以在其原工作区执行一次独立干预；需要保留原报告与工作区。此操作会写入用户澄清规则并对原内容提交复查，不重算首轮分数：

```sh
python -m eval.mail_followups \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/mail-original \
  --first-pass /local-disk/eval/mail-original.json --recheck-existing \
  --output /local-disk/eval/mail-owner-clarification.json
```

这个干预只适用于已经保留该首轮错误的场景，不能通过反复调用筛选出成功结果。相同规则下的重复复查仅复用回执；通用 API 见[使用指南](../docs/handbook/followups.md)。TLS 测试夹具生成临时证书并保留协议命令，不接触真实私人邮箱。

记忆向量资产先通过 `configure-memory --download` 显式准备，参见[配置指南](../docs/handbook/memory-proactive.md)。生产运行不临时下载。中文功能场景与英文公开基准使用各自固定的向量空间，不混库。

## Telegram 链路延迟

纯 Runtime 测量不发送 Telegram 消息；使用与实际运行相同的文件系统，保留冷启动与后续请求的差异：

```sh
python -m eval.telegram_latency \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /same-filesystem/eval/latency-new --output /private/path/latency.json
```

测试实际适配器与 Telegram 发送时，额外指定 `--telegram-token-file /private/path/bot.key --telegram-chat-file /private/path/chat.key --expected-bot your_bot`。两个文件只保存 token、已确认的私聊数字 ID。该模式会向这个私聊发送四条受控回复；隔离工作区关闭轮询，在线实例继续收信。它验证实际 Core 接纳、流式草稿和投递回执，不冒充手机到手机测试。需要新的工作区及独立插件 home，不能连接生产工作区运行夹具。

报告分开记录原始 SSE 正文、公开正文回调、草稿 API 确认、最终回执和各材料阶段；推理字段不算正文。每次结束后顺序重放相同原生请求字节，只存摘要和大小，不保存正文或密钥。先后次序可能预热供应商缓存，因此不能据此计算产品 A/B 加速比。默认头部跟踪支持通用兼容驱动及既有订阅驱动，不依赖具体模型名。

结果、慢样本与开发失败见 [Telegram 实测](../docs/operations/evaluations/2026-10-22-telegram-latency.md)。

## 记忆取证深度

这个受控组件实验测量片段外的原始事实是否恢复，使用固定合成历史、正式记忆组件与真实 Models reader；不是公开 benchmark。向量和图方案都有相同的原文读取机会。原文预算包含序列化说明；累计重发输入、调用数和用量另外报告。

```sh
python -m eval.memory_depth \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/depth-new --output /private/path/depth.json \
  --per-family 4 --candidates 4 --max-chars 6000 --initial-chars 2400 --parallel 3
```

首轮 80 个组合保留格式错误、读取失败和错误回答，不替换已完成结果。末页来源提供正常的目录线索，不向 reader 提供参考编号或正确读取偏移。原文只允许从当前父回执已经入选的消息读取。

保存首轮报告与其 `frozen-corpus/sessions.db` 后，可在另一个新工作区重新核对全部材料摘要、原文范围，并对已冻结回答作一次独立同模型语义复核：

```sh
python -m eval.memory_depth \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/depth-grade-new --output /private/path/depth-grade.json \
  --analyze-existing /private/path/depth.json \
  --corpus-db /local-disk/eval/depth-new/frozen-corpus/sessions.db --parallel 2
```

复核不重跑 reader、不追加原文、不改原报告；先复制一致的 SQLite 快照，judge 看不到方案名称。原机械规则、无效 judge 与新复核分别保存，不能挑高分替换首轮。确定性原文覆盖与有误差的答案评分分开解释。结果见[取证深度实测](../docs/operations/evaluations/2026-10-22-memory-depth.md)。运行实际渠道、工具与完整重启继续使用 `eval.memory_runtime`。

## 工具读取与执行

只测组件时不需要模型凭据；`--baseline-ref` 需要包含该提交的源码 checkout，省略它则只验证当前实现：

```sh
python -m eval.tool_runtime --component-only --baseline-ref fe1387c --samples 3 \
  --workspace /local-disk/eval/tools-components-new --output /local-disk/eval/tools-components.json

python -m eval.tool_runtime --baseline-ref fe1387c --samples 3 \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --workspace /local-disk/eval/tools-agent-new --output /local-disk/eval/tools-agent.json
```

文件测量从 Git 读取已选旧组件，使用相同依赖与输入；报告 Python 分配峰值、带追踪的耗时和逐字节输出对照，不等于整机 RSS 或完整 Harness 性能。HTTP 字节流夹具验证响应头和正文限制；真实模型场景另经正式插件图与本机 HTTP 服务器运行，分别记录模型调用和原生工具阶段。MCP、子任务、调度与恢复继续使用 `eval.native_runtime`，不以这些边界用例替代整个主链。

## 公开历史中的记忆质量

[LongMemEval 官方仓库](https://github.com/xiaowu0162/LongMemEval)和[数据集](https://huggingface.co/datasets/xiaowu0162/longmemeval)另行下载，不把整个历史数据集提交进项目。当前实验文件为 `longmemeval_s_cleaned.json`，SHA256：

```text
d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442
```

每题导入完整 haystack，包括干扰会话；参考答案和 `has_answer` 标签只参与评分。离线导入生成真实 Ledger 消息，由正式 Learning、MessageConsumer 和检索/呈现组件处理；reader/judge 走 Models 服务。这个组件实验与上面的实时渠道验收分别记录。

```sh
python -m eval.memory \
  --dataset /local-disk/data/longmemeval_s_cleaned.json \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-en-v1.5.json \
  --seed kirakira-memory-v1 --per-category 2 --offset 0 \
  --candidates 15 --max-chars 12000 --reader-format text --parallel 2 \
  --systems no-memory recent dense hybrid-rrf akasha akasha-fused \
    akasha-fused-excerpts dense-excerpts hybrid-excerpts \
  --output /local-disk/eval/quality-partition-0
```

使用相同 seed 和 `--offset 2`、`--offset 4` 生成另外两组，各 14 题。按类别内 `SHA256(seed:question_id)` 排序后固定取题，三个分区的题目不重叠；背景历史可能重叠。七类各取六题形成均衡子集，不代表完整基准的类别权重。

所有方案共用编码器、reader、judge、候选上限和包含说明的字符上限。向量基线直接检索闭段的消息向量；混合基线对向量和 BM25 排名融合。原通道、融合与摘录分别保留，不能把加大材料预算当作算法改进。无记忆和近期历史也保留，以检查收益究竟来自哪里。

默认 reader 返回普通文本与消息 ID 引用，独立 judge 按题型判断回答。自定义 judge 不是官方评测器；记录同模型评分的限制。检索指标包括支持会话与带答案标签的消息覆盖；消息覆盖不保证片段包含答案。引用 ID 合法不保证每句话都由证据支持。

失败尝试保留在分母，评分无效也按失败计；不因重试而覆盖已经完成的答案。`--resume` 只继续未启动的组合。`--reuse-index DIR` 可复用同一数据/向量空间下的冻结索引，使用 SQLite backup 包含已提交 WAL，并核对图摘要。

同一私有端点的评测通过跨进程共享槽限制并发；这只管理评测请求，不进入产品 Runtime。

完成后可在相同条件的命令上增加 `--resume --verify-evidence`：不调用 reader/judge，重新检查候选、入选消息和材料长度，保存来源范围及材料摘要。首次运行已记录摘要时还核对字节摘要；历史运行缺少摘要时，不能追溯证明它当时的材料字节完全相同。

合并完整分区与探索记录：

```sh
python -m eval.memory_report \
  --runs /local-disk/eval/quality-partition-0 /local-disk/eval/quality-partition-2 /local-disk/eval/quality-partition-4 \
  --exploratory /local-disk/eval/exploratory-run \
  --output /local-disk/eval/quality-report.json
```

合并器拒绝不完整主实验、重复题目、不同数据/预算/模型条件；输出逐题结果、分项、配对胜负和调用账本。探索实验与主实验分开，不混合不同 reader 格式。

### 已发布记忆报告重算

公开报告不依赖私有模型工作区即可重算；以下命令只读取 JSON，不调用模型，也不修改原始记录：

```sh
python -m eval.memory_report \
  --from-report docs/operations/evaluations/artifacts/2026-10-21-memory-runtime/memory-quality.json \
  --output /local-disk/eval/memory-findings.json
```

输出重新核对主实验的完整性、分区、预算和已发布汇总，区分可回答题与无法回答题、带引用回答与空引用，并列出补全通道标签消息在各方案中是否实际呈现。`completion_only_gold_messages` 是补全通道归属，不能据字段名称推断完整向量方案一定找不到。探索记录不进入主实验分母；来源报告和重算代码均保存摘要。

## 状态一致性和确定性链路

```sh
python -m scripts.message_wake_scenario
python -m scripts.scheduler_tools_scenario --output /local-disk/eval/scheduler-new
python -m scripts.ledger_runtime_scenario
python -m eval.upstream.akasha_cost_scenario --source . --output /local-disk/eval/state-new
python -m eval.upstream.akasha_publish_replay_scenario --help
python -m eval.upstream.akasha_derived_recovery_scenario --help
```

这些入口检查提交、授权、调度、物理退出、图发布、消费进度和重建。Scheduler 使用本地确定性模型 HTTP 对端。记忆状态场景使用合成向量，适合证明一致性，不能证明真实语言理解或公开数据集上的质量。

当前结果、保留的失败与限制见[验证入口](../docs/operations/verification.md)。
