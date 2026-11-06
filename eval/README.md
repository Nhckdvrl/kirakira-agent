# 评测

评测使用项目同一个 `.venv` / Python 3.14.6。工作区、数据集、模型权重和临时结果放仓库外的本机磁盘；正式结果统一进入 `docs/operations/`。安装依赖后，下面的命令使用 `--no-sync` 沿用同一环境，不调用系统 Python。

```sh
uv sync --frozen --extra providers --extra memory
```

## 模型条件

`followups --chat --chat-scenario exhibit|lesson` 使用不同的生活与兴趣材料。
用户请求不包含工具名称，检查实际工具结果与文件、规则和投递状态。
一次性任务、未配置主动联系、跨会话更正、新事件判断、规则变更、暂停和重启分别记录。
`--investigation-timeout` 默认 600 秒；这是功能验收等待上限，实际耗时另记，不代表响应时延目标。
模型连接失败保留每次尝试，开发失败不通过重新运行覆盖。来源为受控的本地 HTTP 页面，通知只发到隔离本地会话。

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

## 召回开销

不需要聊天模型和外部账户，固定向量与真实记忆组件重放连续回合，分别报告每轮注入字符、重复注入和正文／索引构成：

```sh
uv run --frozen --no-sync python -m eval.recall_cost \
  --seed-turns 12 --live-turns 15 --filler-chars 2000 \
  --output /local-disk/eval/recall-cost.json
```

它只衡量开销，不衡量回答质量：注入变少不等于模型仍能找到所需原文，两者分别验收。

## 一条命令重算全部免密钥证据

[为什么不用现成的](../docs/architecture/why-not-off-the-shelf.md)那一页上所有不需要密钥的数字，由一条命令重算出来——设计性质对照、主动交互的反馈边界、以及每轮召回开销：

```sh
uv run --frozen --no-sync python -m eval.offline_evidence \
  --output /local-disk/eval/offline-evidence.json
```

它不自己做测量，只把三组现成的套件跑一遍并排成一张表（`memory_guarantees`、`proactive_guarantees`、`cost_report`），每格都带对照臂，输出里同时保留三份原始报告供逐项核对。`--learn-turns` / `--queries` 控制其中唯一昂贵的引擎那段，机器忙时可以调小。

**它量的是设计性质与开销，不是回答质量**：公开基准上本项目与朴素向量检索等价，那条线由 `eval.memory` 用真实模型单独跑。

### 只要开销那一段

不需要 API 密钥、聊天模型或私有工作区；固定向量与真实记忆组件，每项重复若干次并给出中位数与范围：

```sh
uv run --frozen --no-sync python -m eval.cost_report --runs 3 \
  --output /local-disk/eval/cost-report.json
```

输出是一张可直接核对的表（召回每轮注入、正文与索引来源条数、学习耗时、检索均值、峰值分配）。拿到仓库的人可以自己复现这些数字；它只衡量开销，质量由 `eval.memory_depth` 用真实模型单独对照。

## 设计性质对照：和现成检索记忆比什么

回答准确率上本项目与朴素向量检索分不出差别（配对检验 p = 0.29），所以这条线量的是别的东西：回执设计本身给出什么。同样不需要密钥或聊天模型。

```sh
uv run --frozen --no-sync python -m eval.memory_guarantees   --seed-turns 30 --live-turns 25 --output /local-disk/eval/guarantees.json
```

对照基线是最常见的做法：把本轮问题做向量检索取最近邻若干条、正文贴进提示词、不记录这一轮给过什么。两边跑同一批消息与同一组向量，比的是设计不是检索质量。报告给出五项：索引增长后同一请求重渲染是否逐字节一致；越权读取（未呈现过／跨 scope／伪造 ref）是否被拒而合法读取仍放行；每次召回是否带可追责来源；请求里已有的原文是否仍被重复注入；呈现材料是否都是真实原文的逐字节切片。

限度与基线的公平性写在报告的 `claim_limits` 和 `caveat` 里：这不等于防注入，模型发起新查询仍可在同一 scope 内浮出新原文；现成方案也完全可以自己缓存每轮材料。完整论证见[为什么不用现成的记忆方案](../docs/architecture/why-not-off-the-shelf.md)。

## 主动交互的反馈边界：穷举而非抽样

关注权重是个纯函数，所以"会不会被反馈带跑偏"这个问题可以彻底回答，不需要抽样：

```sh
uv run --frozen --no-sync python -m eval.proactive_guarantees --limit 40 \
  --output /local-disk/eval/proactive-bounds.json
```

对照是最自然的写法——直接用"有用"评价占比当权重。报告给出：在全部评价状态里权重是否归零（归零即永久消音，该来源再也越不过唤醒阈值）、是否越出 `[0.5, 1.5]`、是否在证据不足 4 条时就改动、以及"有用"占比上升时权重是否单调不降。另给一组对抗性 payload（超大、越界、负数、`inf`、`NaN`、布尔、字符串、缺失），检查构造的订阅内容能否把自己的权重抬高——判为中性而不是截断到边界，因为截断仍然承认了它想要的方向。

这一组只量边界与单调性，**不量判断质量**；主动联系该不该发由受控邮件实验单独衡量。

## 记忆开销

不调用聊天模型，固定向量与真实学习／检索组件，分别报告学习耗时、检索延迟和峰值分配：

```sh
uv run --frozen --no-sync python -m eval.memory_cost \
  --turns 150 --queries 15 --output /local-disk/eval/memory-cost.json
```

单次运行离散度较大，比较前后应各跑三次并报告中位数与范围。它只衡量开销，不衡量检索质量。

安装了 `memory` extra 时编译内核生效，检索明显更快但峰值更高、每进程首次使用要编译一次；对比不同环境的数字前先确认 `numba` 是否可用。

## 反馈离线回放

不调用模型、不联系任何人，只读打开工作区的 `sessions.db`，重算每个跟进的关注权重并报告会改变哪些联系决定：

```sh
uv run --frozen --no-sync python -m eval.feedback_replay \
  --store /local-disk/workspace/sessions.db --holdout 0.2 \
  --output /local-disk/eval/feedback-replay.json
```

`--holdout` 按稳定哈希留出一部分评价，供之后用未参与权重的评价单独评分。决定翻转不等于改善。

## 功能验证

每次使用不存在或全新的隔离工作区：

```sh
uv run --frozen --no-sync python -m eval.native_runtime \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --workspace /local-disk/eval/core-new --output /local-disk/eval/core.json

uv run --frozen --no-sync python -m eval.native_alignment \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/features-new --output /local-disk/eval/features.json

uv run --frozen --no-sync python -m eval.memory_runtime \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/memory-new --output /local-disk/eval/memory.json

uv run --frozen --no-sync python -m eval.native_web \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --workspace /local-disk/eval/web-new --output /local-disk/eval/web.json

uv run --frozen --no-sync python -m eval.followups \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/followups-new --output /local-disk/eval/followups.json \
  --isolated --long-source

uv run --frozen --no-sync python -m eval.followups --chat --chat-scenario exhibit \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/chat-new --output /local-disk/eval/chat.json

uv run --frozen --no-sync python -m eval.mail_followups \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/mail-new --output /local-disk/eval/mail.json \
  --with-functional

uv run --frozen --no-sync python -m eval.runtime_settings \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/settings-new --output /local-disk/eval/settings.json

uv run --frozen --no-sync python -m eval.personality \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --workspace /local-disk/eval/personality-new --output /local-disk/eval/personality.json
```

| 入口 | 验证范围 |
| --- | --- |
| `native_runtime` | 生产插件图中的对话、文件、Shell、MCP、子任务、取消、恢复、附件、调度、投递，另含搜索后读取原文和步数耗尽后新问题恢复 |
| `native_alignment` | 在线记忆学习、诊断、事件跳过/投递/去重、实际插件安装、压缩与重启 |
| `memory_runtime` | 真实渠道学习到查询、RPC、Agent 工具、原文展开、项目提醒、显式反馈和重启 |
| `native_web` | 认证后的工作台/聊天 HTTP、import map、项目作用域与实际文件工具 |
| `followups` | 原始对话创建跟进、真实 HTTP 订阅/网页差异、调查分享/跳过、精确投递、反馈、暂停与恢复；可选隔离项目和长原文读取 |
| `followups --chat` | 不点名工具的网页整理、文件核对、提醒、跟进创建/更正/暂停/恢复；跨会话条件改变新事件判断；核对实际回执 |
| `mail_followups` | 本地 TLS IMAP 的真实采集、跨会话历史条件与分享/跳过、线程更正、规则修改、外部指令、UID 重建与重启 |
| `runtime_settings` | 实际认证 HTTP 设置、原生配置换代、越界拒绝、清空恢复默认、真实记忆工具预算与图不重学、重启保持 |
| `personality` | 四种组合的真实闲聊、纯数字、严格 JSON、文件写入与复读、临时要求、未知偏好及重启；记录实际请求中的配置片段，另做匿名风格匹配 |
| `prompt_cache` | 稳定/变化前缀的受控机制实验；原生邮件调查的实际请求摘要、缓存用量和功能结果；缓存字段未知与零值分开 |
| `idle_reflection` | 空闲规划、跨会话约束、原文依据、原生研究/投递、目标完成后跳过、预算、暂停及交接回执丢失恢复 |
| `tool_runtime` | 文件与网页读取边界、同输入组件测量，以及分页、编辑、目录、并行、Web 恢复、PTY 和大输出的真实 Agent 验收 |
| `runtime_experience` | 本地服务分段耗时、SDK/CLI、真实网页切换与引用交互、原生存储比较、跨会话长原文和后来更正 |

`native_runtime` 的搜索项连接本地 SearXNG 协议夹具，搜索摘要不含答案，必须实际读取结果页面，
再核对日期、时区、随机校验码和链接；它验证提供方选择与原生工具链，不评价公共搜索引擎质量。
步数恢复项将回复上限临时设为一次模型请求，检查工具已有回执后的自动终止、下一句正常回答和不重复执行。
`native_alignment` 的压缩项把未结束来源放在较早位置，检查后续完整历史仍被身份集合摘要覆盖，原始日志保持不变。

`memory_runtime` 的事件提醒验收固定不含参考答案的近期关注条件；历史决定由其他真实渠道会话学习。除了提醒内容，还检查 Alert 归档的原事件身份、参考对话范围和实际成功的记忆读取回执，避免把此前确认回复已经说出的答案误算为本轮检索能力。

`memory_runtime --personal-only` 单独运行长原文定位和采购事件提醒两项，工具召回上限固定为 2400 字符；SDK 原文检索的 JSON 输出也有单独上限。它验证召回线索之后能继续定位真实原文，不是自主检索策略的公开质量基准。原始全文、查询、窗口与实现摘要冻结后，可以用 `--verify-source-search <证据.json> --output <新文件.json>` 离线重算当前窗口，不需要工作区、模型或密钥；冻结基线代码只核对摘要，不自动执行。

`personality` 的组合保存沿实际宿主配置换代，所有对话仍走正式 Agent 链路。`--style-only` 只重测配置、闲聊与重启，`--examples-only` 定向检查可选说话示例不会变成事实、干扰计算或替代真实工具执行。`--judge-existing <结果.json>` 复用冻结的回复，重新评价时不重新生成答案。评审的 `--judge-repeats` 默认 2，匿名选项逐次乱序，失败与无法判断也保留在分母中。评审输出与推理预算由 `--judge-output-tokens` 单独设置，默认 8192，不改变 Agent 回答预算；记录截断、未调工具等失败。风格匹配与任务正确性分别报告；同模型评审不是独立的人类体验评价。

`--chat-suite development|transfer` 切换固定话题。development 用于开发迭代，transfer 用新的日常话题作迁移检查；它们都不是公开人格基准。除字数与问号，还保留评审摘录的隐含问题和缺少依据的个人断言，避免把“不出现问号”误写成“没有追问”。

`--controls-only` 在其余五项和输入固定时，分别改变展开、追问和幽默，检查同一数学结果、长度变化和追问变化；幽默仅保留对照原文，不自动打分。该实验每项只有一对样本，与完整组合的风格辨识分开报告。

`--behavior-only --chat-suite development|transfer|validation` 固定六个表达维度，只改变支持或建议偏好。每组另检查同一会话中的倾诉、明确要办法、精确计算、真实文件工具及重启；这些功能断言不等于行为符合率。观察评审只见用户原话与实际回复，不见目标配置，逐字摘录建议、问题和无依据断言。`--judge-behavior-existing <结果.json>` 只重评冻结回复，不重新生成答案。保存时明确清空实验未选字段，相同配置直接复用，避免上一组条件残留；原始协议失败与修正后的独立评审均保留。

个人伙伴的空闲思考使用独立工作区验证：

```sh
uv run --frozen --no-sync python -m eval.idle_reflection --implicit-goal --fault-cases \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/idle-new --output /local-disk/eval/idle.json
```

`--implicit-goal` 将预算、时间条件放到另一段历史会话，当前只保留未完成话题；没有创建结构化定时任务或提交外部事件。它检查规划实际呈现的历史出处、研究中的记忆工具与真实 HTTP 页面、正确约束和确认投递。`--fault-cases` 在真实生产端写入原生 Drift 提案之后注入一次交接回执丢失，检查重放复用原身份与规划调用，再重启、恢复 Wake 并核对实际结算。已有相同研究结果时，恢复后可以跳过重复通知。两者都是受控功能实验，不能推导日常主动消息的实际有效率或图记忆相对其他检索的净提升。

`--conversation-topic painting|reading|music|gardening` 改为用户明确邀请的日常话题接续，实际等待五分钟空闲；检查有出处的 conversation 提案、实际记忆读取、分享或成功的跳过决定、同输入去重、完成后保持安静、预算、暂停和重启。没有要求每次都通知，也不把投递数当成满意度。每个话题使用独立的新工作区。

缓存机制与真实链路分别运行，均使用新的本地工作区：

```sh
uv run --frozen --no-sync python -m eval.prompt_cache --mode probe --rounds 8 \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --workspace /local-disk/eval/cache-probe-new --output /local-disk/eval/cache-probe.json

uv run --frozen --no-sync python -m eval.prompt_cache --mode mail --attention-cases \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/cache-mail-new --output /local-disk/eval/cache-mail.json
```

请求跟踪目前支持 `openai-compatible` 和 `step-plan` 驱动；它观察实际插件代中的传输，不修改生产 Prompt、工具目录、请求路由或流式选择。报告只存请求摘要和大小，不存请求正文或凭据。`mail` 另存 `.functional.json`，保留原有功能验收结果；`--attention-cases` 在原有 8 项之外增加 4 项一般知识、话题、补材料和限时确认的内容判断，分别核对跳过与实际投递。字符前缀只是本地诊断，不是供应商的实际 token 前缀。机制实验可以说明缓存复用条件，不能替代产品优化前后的效果和费用对照。

`eval/fixtures/native_extension` 提供真实托管 HTTP 进程和 stdio MCP 服务；事件投递目标为隔离的本地会话。它们是受控集成验收，不是外部账号、所有设备或所有可选提供方的部署认证。

`followups` 的本机订阅源提供变化事实，不携带参考答案或人工兴趣分。明确订阅通过独立的调查请求接纳；语义兴趣仍如实记录。`--isolated` 验证调查范围与通知位置分离，`--long-source` 明确要求摘录后展开原文，检查工具可用性；它不是自主选择检索策略的质量基准。加 `--webpage` 将前两条变化切换为真实 HTTP 网页的前后版本，仍经过相同调查、记忆、投递与恢复链路。端到端用例不计作公开通知有效率。

邮件实验用六个固定场景，每个场景的邮件相同，在另一个历史对话中分别放置应分享或应跳过的用户条件。每个条件另有移除该历史的观察组，共 24 次调查；通知收件位置独立于项目隔离。`--family-start` / `--family-end` 可拆分场景，`--functional-only` 只跑功能验收。无历史组本来就缺少个人条件，不能把它当作公平的其他记忆系统基线或现实通知准确率。首轮错误保留，功能断言与行动符合率分别报告。

`--initial-review-only` 单独检查创建前已存在的邮件：以普通中文交办，按本轮原始输入对应的真实创建工具回执找到关注，核对文件夹、回查数量、原话规则与所属会话，不再额外调用 RPC 创建第二份关注。默认只监听新邮件，明确授权后从指定箱回查最近一封；Junk 中的补材料要求与 INBOX 中的普通推广分别作一次真实模型判断，核对日期、时区、来源 UID、重复 poll 和原生重启后的回执。通知后再用普通中文确认截止前一天的提醒，查真实调度回执、时间与重启状态；不等待未来触发来虚报投递成功。作用已提交而最后确认回复失败时，仍追踪并清理该关注，回复失败保留。两项判断与提醒登记分开计数，全部失败保留，不据此发布垃圾邮件分类准确率。

自动监测另有两条真实模型验收，均使用隔离工作区与本地合成来源：

```sh
uv run --frozen --no-sync python -m eval.followups --monitoring \
  --workspace /local-disk/eval/tickets-new --output /private/path/tickets.json \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json

uv run --frozen --no-sync python -m eval.mail_followups \
  --functional-only --live-monitoring --attention-cases \
  --workspace /local-disk/eval/mail-idle-new --output /private/path/mail-idle.json \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json
```

`--monitoring` 固定六项计划：普通中文交办、显式首次现状检查、未开售时跳过、只改变按钮禁用状态后的自动发现与实际送达、关注区外变化不调查，以及暂停后重启保持。全程不手动触发采集。新事件按快照摘要匹配，调查核对最新原生 Wake 阶段、领域结算与准确通知身份；静默检查等待对应 HTTP 版本的采集检查点提交。开票变化的发现耗时和包含模型调查的送达耗时分别报告，首次检查从用户请求提交前计时。这不是对真实售票站、抢票或 Telegram 的验收。

`--live-monitoring` 必须与 `--functional-only` 一起使用，开启真实本地 TLS IDLE；先用 RPC 配置关注并建立 UID 基线，定时兜底为一小时，发布新业务邮件后不手动调用 poll。每次自动接纳核对实际 UID、Message-ID 和文件夹，再检查完整分享／跳过及投递。UID 身份重建场景仍有两次明确的手动检查，与新邮件自动接纳分开记录；10 次重复／flag 信号必须等采集检查点完成后仍无新事件或模型调用。含 `--attention-cases` 固定十五项工程与模型验收，实际内容判断另有八项分母。模型、采集、投递和真实用户重要性不能合并成一个准确率。

只检查真实本地 TLS 协议与采集游标时，不需要模型、密钥或向量配置：

```sh
uv run --frozen --no-sync python -m eval.mail_followups --protocol-only \
  --workspace /path/to/isolated-mail-eval --output /path/to/private/protocol.json
```

该模式验证实际 UID 有间隔时的最近 12 封窗口、计划先冻结、每批 10 封、回查中新到邮件随后追上、目录隔离、UID 身份变化、同内容去重与 `EXAMINE` / `BODY.PEEK`。JSON 游标往返不冒充 Runtime 重启；原生恢复由故障回归和真实模型验收另查。不访问私人邮箱，也不改变邮件状态。

已有首轮出现地域条件误解时，可以在其原工作区执行一次独立干预；需要保留原报告与工作区。此操作会写入用户澄清规则并对原内容提交复查，不重算首轮分数：

```sh
uv run --frozen --no-sync python -m eval.mail_followups \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/mail-original \
  --first-pass /local-disk/eval/mail-original.json --recheck-existing \
  --output /local-disk/eval/mail-owner-clarification.json
```

这个干预只适用于已经保留该首轮错误的场景，不能通过反复调用筛选出成功结果。相同规则下的重复复查仅复用回执；通用 API 见[使用指南](../docs/handbook/followups.md)。TLS 测试夹具生成临时证书并保留协议命令，不接触真实私人邮箱。

记忆向量资产先通过 `configure-memory --download` 显式准备，参见[配置指南](../docs/handbook/memory-proactive.md)。生产运行不临时下载。中文功能场景与英文公开基准使用各自固定的向量空间，不混库。

## 本地使用与延迟

`service` 默认只读健康、会话目录与 UI 资源；`--exercise` 才会发送四条 SDK 和两条 CLI 合成消息。
这些会话排除记忆学习，不在真实用户的聊天中伪造输入。报告区分接纳、首次可见正文、完成、模型回执及可选 journal 阶段；
模型的 first token 可能是推理，并不等于用户看到正文。进程 CPU 是整个 worker 在测量窗口的增量，后台活动可能参与其中。
每条 SDK/CLI 结果的 `failure_http_statuses` 只提取该合成轮次持久 `Control` 失败中的 HTTP 状态号码，
不发布错误原因正文；CLI 的 exit 1 可以与实际模型服务拒绝对应。这个字段不覆盖成功之前的全部内部重试。

```sh
uv run --frozen --no-sync python -m eval.runtime_experience service \
  --workspace /local-disk/workspace --exercise --output /local-disk/eval/service.json

uv run --frozen --no-sync python -m eval.runtime_experience storage \
  --nfs-directory /nfs/private-scratch --local-directory /local-disk/private-scratch \
  --samples 24 --output /local-disk/eval/storage.json

uv run --frozen --no-sync python -m eval.runtime_experience mechanism \
  --nfs-directory /nfs/short-eval --local-directory /local-disk/short-eval \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --memory-documents /private/path/memory-snapshot \
  --output /local-disk/eval/io-mechanism.json

uv run --frozen --no-sync python -m eval.runtime_experience references \
  --workspace /local-disk/eval/ref-new \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --output /local-disk/eval/references.json

uv run --frozen --no-sync python -m eval.runtime_experience background \
  --workspace /local-disk/eval/background-new \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --output /local-disk/eval/background.json

node node_modules/vite/bin/vite.js build --config frontend/chat/vite.harness.config.ts \
  --outDir /local-disk/eval/chat-assets
uv run --frozen --no-sync python -m playwright install chromium
uv run --frozen --no-sync python -m eval.runtime_experience browser \
  --workspace /local-disk/eval/web-new --assets /local-disk/eval/chat-assets \
  --latency-ms 80 --output /local-disk/eval/browser.json
```

`storage` 只创建、测量并删除自己的临时目录，不写生产库。它同时测 FULL/DELETE 小事务、原生 Ledger 的 WAL/NORMAL 写入、缓存读取及文件 fsync，
前两种策略的数字不能混用。目录标签不检测实际文件系统，应另用系统挂载信息核对；这也不是冷缓存或吞吐基准。
正式工作区不应迁入可清理的临时目录。

`mechanism` 在两种目录下各建一个短路径的临时工作区，顺序运行冷、暖两个请求，结束后清理自己的目录。
它把确定性 HTTP 模型绑定在 loopback，经正式 Models driver 和原生插件图运行，并复用 `telegram_latency` 的阶段与 I/O 诊断；
没有外部聊天 API 或真实凭据。需要预先准备本地向量资产，可选的 `--memory-documents` 只读复制 Markdown 快照，
不复制正式消息或图数据库。夹具固定输出和一输入、一输出 token 均为合成值，只能定位调度与存储机制，不能当作语言、记忆质量或费用结果。
父目录必须足够短以容纳 Linux Unix socket；`stat` 可用时报告实际文件系统类型。原生子报告可能含 Markdown 文件名，公开前仍需白名单脱敏。

`references` 用真实模型与相同 SDK 输入片段，核对长消息后文、原会话的新决定与原始前缀不变；记录坐标字节、窗口、实际工具与模型用量，不发布回答或推理正文。

`background` 使用不点名工具的中文交办，资料由实际本地 HTTP 服务暂缓返回：核对持久接纳与公开确认、等待资料时继续正常聊天、实际原文结果、唯一完成通知与投递回执、自然语言查询原始结果，以及重启后再次查询。固定六项计划，配置失败或依赖失败后的未运行项也保留，CLI 返回非零。成功工具调用按实际 CallRef、任务身份和原始结果窗口核对；不能只看模型声称已使用工具。门控预算与隔离工作区的网页等待上限为 120 秒，不修改生产默认；报告实际等候及夹具超时。通知与任务结果分别检查日期、时间、时区、材料和核验码，使用本地会话送达确认，不代表 Telegram 或用户已读，也不是公开基准与群体任务成功率。

`browser` 无需配置模型即可用 Chromium 验证真实原生后端上的切换、拖入引用、去重、草稿隔离、移除和自引用拒绝。
`--comparison-assets` 可指定另一份编译好的界面，后端和合成会话保持相同；网络参数是 Chromium 模拟设置，不是实测 RTT。
可用 `--browser-executable` 选择已有 Chromium。网页测试工作区必须是新目录，Linux Unix socket 路径不能过长。

Telegram 的真实发送与相同请求重放仍使用下面的 `telegram_latency`。重放的 `network_events` 只记连接阶段名称和时间，不记录请求头、密钥或正文；
失败的重放保留 HTTP 状态，不能算作一次成功的直接模型请求。模型配置阶段的拒绝会退出非零，不能把空报告当作通过。

当前实例与硬件观察见[使用体验报告](../docs/operations/evaluations/2026-10-31-runtime-experience.md)。


## Telegram 链路延迟

纯 Runtime 测量不发送 Telegram 消息；使用与实际运行相同的文件系统，保留冷启动与后续请求的差异：

```sh
uv run --frozen --no-sync python -m eval.telegram_latency \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /same-filesystem/eval/latency-new --output /private/path/latency.json
```

测试实际适配器与 Telegram 发送时，额外指定 `--telegram-token-file /private/path/bot.key --telegram-chat-file /private/path/chat.key --expected-bot your_bot`。两个文件只保存 token、已确认的私聊数字 ID。该模式会向这个私聊发送四条受控回复；隔离工作区关闭轮询，在线实例继续收信。它验证实际 Core 接纳、流式草稿和投递回执，不冒充手机到手机测试。需要新的工作区及独立插件 home，不能连接生产工作区运行夹具。

可用 `--case natural-greeting` 单独测试原话“你好”，不在输入中限制工具。它要求正常回应且没有无关工具调用，
避免只用“不要调用工具”的指令测试闲聊。`--case` 可重复选择场景，省略时仍为原四项。
`--memory-documents /private/path/memory` 只把 Markdown 快照读入隔离工作区并记录摘要；不会复制生产图、
会话、凭据或轮询配置。这种测试不代表复现了生产记忆召回；复现历史干扰需另外冻结实际召回材料。

加入 `--case natural-followup --no-direct-replay` 可连续检查“你好”和“在吗”，两轮之间不额外重放模型请求。
将隔离工作区放在生产同类文件系统上，才能观察相应磁盘开销。报告只记录各角色正文、推理字段与工具 schema
的字符数和实际用量；字符数不是 token 数。默认模式仍保留原同字节重放诊断。

需要调查文件系统与事件循环时，增加 `--file-io-trace`。它记录有界文件 worker 的等待、物理工作和整个 `await` 的墙钟时间，
并每 20 ms 采样主线程栈；不记录操作参数、正文或文件路径。`work_ms` 可能包含锁、CPU 与文件系统等待，
`duration_ms` 超过 `queue_ms + work_ms` 的部分也可能是事件循环恢复延迟，不能直接称为磁盘时间。
主线程采样计数用于定位阻塞调用，不是精确函数计时。比较存储时使用新隔离目录和相同模型、资产及快照，
保留首次与后续请求；不要为了获得更快数字把正式工作区移到临时目录。

`--case context-maintenance --no-direct-replay` 在软水位放入含更正日期、负责人、数据限制和未完成工作的
合成历史，核对先回答、后整理，以及原文和开放工作不被压缩覆盖。报告的 `native_total_ms` 截至回复或
投递确认，`native_summary_driver_ms` 与 `maintenance_wait_after_reply_ms` 另计后台整理；
`model_calls` 包含随后完成的摘要调用。`--case context-compaction` 则直接超过硬容量，检查必须等待压缩时
仍能恢复这些事实；两种场景不能互相替代。

报告分开记录原始 SSE 正文、公开正文回调、草稿 API 确认、最终回执和各材料阶段；推理字段不算正文。每次结束后顺序重放相同原生请求字节，只存摘要和大小，不保存正文或密钥。先后次序可能预热供应商缓存，因此不能据此计算产品 A/B 加速比。默认头部跟踪支持通用兼容驱动及既有订阅驱动，不依赖具体模型名。

结果、慢样本与开发失败见 [Telegram 实测](../docs/operations/evaluations/2026-10-22-telegram-latency.md)。

## 记忆取证深度

这个受控组件实验测量片段外的原始事实是否恢复，使用固定合成历史、正式记忆组件与真实 Models reader；不是公开 benchmark。向量和图方案都有相同的原文读取机会。原文预算包含序列化说明；累计重发输入、调用数和用量另外报告。

```sh
uv run --frozen --no-sync python -m eval.memory_depth \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/depth-new --output /private/path/depth.json \
  --per-family 4 --candidates 4 --max-chars 6000 --initial-chars 2400 --parallel 3
```

首轮 80 个组合保留格式错误、读取失败和错误回答，不替换已完成结果。末页来源提供正常的目录线索，不向 reader 提供参考编号或正确读取偏移。原文只允许从当前父回执已经入选的消息读取。

保存首轮报告与其 `frozen-corpus/sessions.db` 后，可在另一个新工作区重新核对全部材料摘要、原文范围，并对已冻结回答作一次独立同模型语义复核：

```sh
uv run --frozen --no-sync python -m eval.memory_depth \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/depth-grade-new --output /private/path/depth-grade.json \
  --analyze-existing /private/path/depth.json \
  --corpus-db /local-disk/eval/depth-new/frozen-corpus/sessions.db --parallel 2
```

复核不重跑 reader、不追加原文、不改原报告；先复制一致的 SQLite 快照，judge 看不到方案名称。原机械规则、无效 judge 与新复核分别保存，不能挑高分替换首轮。确定性原文覆盖与有误差的答案评分分开解释。结果见[取证深度实测](../docs/operations/evaluations/2026-10-22-memory-depth.md)。运行实际渠道、工具与完整重启继续使用 `eval.memory_runtime`。

### 窗口规则的答案质量

召回把"请求已经引用的原文"改成只给一行索引之后，必须单独回答一个问题：**省下的预算有没有换来更差的答案。** 这个套件把两种呈现放在同一语料、同一候选集、同一初始预算和同一读取额度下对照，只有初始呈现不同：

```sh
uv run --frozen --no-sync python -m eval.memory_depth --suite window-v1 \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/window-new --output /private/path/window.json \
  --per-family 4 --candidates 4 --initial-chars 2400 --max-chars 6000 --parallel 3
```

`window-bodies` 按窗口规则之前的方式给出片段正文；`window-index` 把每个候选都当成"原文已在请求里"，withhold 正文、对放得下的花一行索引，和现在的召回一致。两臂都允许最多两次 `read_memory_source`，因此索引臂必须真的沿 `ref` 去读原文才能答对——这同时验证了索引行的读取准入。

夹具用 `source-followup` 的第 3 版。第 2 版把承载答案的那一行改名成问题里不出现的字段（`归档栏`），为的是不让 reader 靠关键词命中；末页题和中部题还能通过目录说明一步推出来，首部题却没有目录说明，原文里没有任何一句把它和问题问的"正式签收凭证号"连起来，而另一条更早的消息明确说"尚无正式签收凭证号"——那四题答 UNKNOWN 比答编号更有依据，等于没在测东西。第 3 版保留改名（仍然要导航），但在每份报告开头用一句普通正文写明哪个字段登记它，既不给值也不给位置。`tests/test_eval_harness.py` 把这条"答案必须能从原文确定"钉成回归，同时锁住第 1、2 版夹具的摘要，使已发布报告仍可从源码复现。

评分沿用严格规则：答案必须等于参考编号（或 UNKNOWN），引用必须指向本次查询确实呈现过的原文，且被引证据要包含该编号。报告里的 `initial_material_chars` 与 `initial_material_sha256` 同时给出两臂各自花掉多少、以及材料确实不同。

报告里有两类并列的分母，不要混读：

- `reference_reachable / answerable`：**不经过模型**的确定性判定——参考编号是否确实出现在本次回执准入可读的原文里。它回答"省掉正文是推迟了还是丢了"。
- `answerable_correct / answerable` 与 `answerable_correct_of_answered / answerable_answered`：前者分母是全部可回答题，后者只算 reader 真的返回了函数调用的那些。`protocol_failures` 给出"没有返回恰好一个函数调用"的次数——那不是答错，是没答；混进答错会让小分母被供应商行为主导。
- `protocol_failures` 里还要再分两种，报告用 `truncated_calls` 与 `prose_calls` 分开给出：`finish_reason` 为 `length` 的那次是**被输出预算截断**，推理型 reader 在发出函数调用之前就把 token 花完了，这是评测自己的配置问题；`finish_reason` 正常结束却仍返回散文的那次，才是 reader 不肯按要求调用。把前者记成模型表现会把自己的配置失误读成模型结论。

`--reader-output-tokens` 控制 reader 每次调用的输出预算，默认取模型配置里的 `max_output_tokens`。推理型模型的思考占用同一预算，预算偏小就会在发出那一个函数调用之前被截断；早期本套件把它硬钉在 1024，于是把截断记成了协议失败。

它只覆盖一个合成导航夹具和一个模型，不能证明窗口规则对"答案就在初始材料里"的对话同样无损。

本轮两格都有数了。确定性那格：初始材料 2390 → 820 字，参考值可达两臂同为 12/12。回答质量那格：严格答对正文臂 **9/12**、索引臂 **6/12**——在这个刻意把答案放到初始材料之外的夹具上，少给正文会掉答对率，结论照原样写在报告里。两臂都没有编造过编号，失手是"没答"或"答 UNKNOWN"；索引臂输入 token 反而更低（50,839 对 54,356）。

拿到这格之前先修了三处取证缺陷，其中两处会把评测自己的毛病读成模型结论：输出预算钉在 1024 把推理型 reader 的截断记成协议失败；夹具第 2 版的首部题无法从原文确定答案，两臂恒为 0/4。诊断与前后对照见[窗口规则](../docs/operations/evaluations/2026-11-06-memory-window.md)。

### 记忆场景亮点

以“初次只记得线索，还能否继续查到原始细节”为目标，运行固定相同初始材料的能力对照。它允许按需组追加材料和调用，分别记录代价；结果不用于整体排名。

```sh
uv run --frozen --no-sync python -m eval.memory_depth --suite source-followup-v2 \
  --systems fused-excerpts fused-adaptive \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --workspace /local-disk/eval/source-followup-new --output /private/path/source-followup.json \
  --per-family 4 --candidates 4 --initial-chars 2400 --max-chars 6000 --parallel 3
```

公开证据包含合成原始消息和查询出处，**离线复核无需密钥、模型、向量权重或私有工作区**：

```sh
uv run --frozen --no-sync python -m eval.memory_depth \
  --verify-existing docs/operations/evaluations/artifacts/2026-10-22-memory-scenarios/evidence.json \
  --output /local-disk/eval/source-followup-verification-new.json
```

也可将 `--verify-existing` 指向自己跑出的原始报告，并用 `--corpus-db /local-disk/eval/source-followup-new/frozen-corpus/sessions.db` 读取真实 Ledger 快照。输出必须使用新文件；复核只重新渲染、复读原文与计分，不重跑 reader，不修复失败回答。

场景收益、控制题失败、材料与调用成本见[记忆的场景亮点](../docs/operations/evaluations/2026-10-22-memory-scenarios.md)。`source-followup-v1` 是保留的开发诊断：初始片段差异造成答案可见性变化，不能用于证明原文展开收益。

## 工具读取与执行

只测组件时不需要模型凭据；`--baseline-ref` 需要包含该提交的源码 checkout，省略它则只验证当前实现：

```sh
uv run --frozen --no-sync python -m eval.tool_runtime --component-only --baseline-ref fe1387c --samples 3 \
  --workspace /local-disk/eval/tools-components-new --output /local-disk/eval/tools-components.json

uv run --frozen --no-sync python -m eval.tool_runtime --baseline-ref fe1387c --samples 3 \
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
uv run --frozen --no-sync python -m eval.memory \
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

### 扩到 140 题做等价判定

上面那组每分区 14 题，足以看广泛退化，但分不出两种检索方案的高下。要给出**等价区间**而不是"没测出来"，把 `--per-category` 提到 20（七类各 20，共 140 题），并只保留决定性的三条臂：

```sh
uv run --frozen --no-sync python -m eval.memory   --dataset /local-disk/data/longmemeval_s_cleaned.json   --model-config /private/path/model.json --key-file /private/path/model.key   --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json   --per-category 20 --parallel 4   --systems no-memory dense-excerpts akasha-fused-excerpts   --output /local-disk/eval/parity-140
```

选 `akasha-fused-excerpts` 是因为自动上下文召回在 `plugins/akasha/runtime.py` 里写死 `selection="fused", presentation="excerpts"`——那才是日常对话里真正生效的配置，`akasha`（lanes/whole）只是召回工具的字段默认值。

样本是分批长出来的：先发布一批，在里面看到一条线索，再抽一批**与之不重叠**的来检验那条线索——检验用的数据不能参与过"发现它"。两批最后要合起来读，而手工合并正是样本悄悄变成挑样的地方，所以合并是一条带规则的命令：

```sh
uv run --frozen --no-sync python -m eval.pool_reports \
  docs/operations/evaluations/artifacts/2026-11-06-memory-parity/evidence.json \
  /local-disk/eval/parity-held-out/result.json \
  --output /local-disk/eval/pooled.json
```

它强制两条：**输入不得重叠**（否则同一题被数两次，直接报错），**只有两臂都答过的题才进配对检验**（单臂答过的被剔除并计数）。它只读已保存的报告重算，不调用任何模型。

读结果时**先剔除 abstention**：不给历史的系统没有任何可用信息，天然会答"不知道"，20 道弃答题它对 19 道；混进总分会让它看起来有 13.6% 的成绩，而它在可回答题上是 0。结论与完整条件见[公开基准 140 题](../docs/operations/evaluations/2026-11-06-memory-parity.md)。

所有方案共用编码器、reader、judge、候选上限和包含说明的字符上限。向量基线直接检索闭段的消息向量；混合基线对向量和 BM25 排名融合。原通道、融合与摘录分别保留，不能把加大材料预算当作算法改进。无记忆和近期历史也保留，以检查收益究竟来自哪里。

默认 reader 返回普通文本与消息 ID 引用，独立 judge 按题型判断回答。自定义 judge 不是官方评测器；记录同模型评分的限制。检索指标包括支持会话与带答案标签的消息覆盖；消息覆盖不保证片段包含答案。引用 ID 合法不保证每句话都由证据支持。

失败尝试保留在分母，评分无效也按失败计；不因重试而覆盖已经完成的答案。`--resume` 只继续未启动的组合。`--reuse-index DIR` 可复用同一数据/向量空间下的冻结索引，使用 SQLite backup 包含已提交 WAL，并核对图摘要。

同一私有端点的评测通过跨进程共享槽限制并发；这只管理评测请求，不进入产品 Runtime。

完成后可在相同条件的命令上增加 `--resume --verify-evidence`：不调用 reader/judge，重新检查候选、入选消息和材料长度，保存来源范围及材料摘要。首次运行已记录摘要时还核对字节摘要；历史运行缺少摘要时，不能追溯证明它当时的材料字节完全相同。

合并完整分区与探索记录：

```sh
uv run --frozen --no-sync python -m eval.memory_report \
  --runs /local-disk/eval/quality-partition-0 /local-disk/eval/quality-partition-2 /local-disk/eval/quality-partition-4 \
  --exploratory /local-disk/eval/exploratory-run \
  --output /local-disk/eval/quality-report.json
```

合并器拒绝不完整主实验、重复题目、不同数据/预算/模型条件；输出逐题结果、分项、配对胜负和调用账本。探索实验与主实验分开，不混合不同 reader 格式。

### 已发布记忆报告重算

公开报告不依赖私有模型工作区即可重算；以下命令只读取 JSON，不调用模型，也不修改原始记录：

```sh
uv run --frozen --no-sync python -m eval.memory_report \
  --from-report docs/operations/evaluations/artifacts/2026-10-21-memory-runtime/memory-quality.json \
  --output /local-disk/eval/memory-findings.json
```

输出重新核对主实验的完整性、分区、预算和已发布汇总，区分可回答题与无法回答题、带引用回答与空引用，并列出补全通道标签消息在各方案中是否实际呈现。`completion_only_gold_messages` 是补全通道归属，不能据字段名称推断完整向量方案一定找不到。探索记录不进入主实验分母；来源报告和重算代码均保存摘要。

## 状态一致性和确定性链路

```sh
uv run --frozen --no-sync python -m scripts.message_wake_scenario
uv run --frozen --no-sync python -m scripts.scheduler_tools_scenario --output /local-disk/eval/scheduler-new
uv run --frozen --no-sync python -m scripts.ledger_runtime_scenario
uv run --frozen --no-sync python -m eval.upstream.akasha_cost_scenario --source . --output /local-disk/eval/state-new
uv run --frozen --no-sync python -m eval.upstream.akasha_publish_replay_scenario --help
uv run --frozen --no-sync python -m eval.upstream.akasha_derived_recovery_scenario --help
```

这些入口检查提交、授权、调度、物理退出、图发布、消费进度和重建。Scheduler 使用本地确定性模型 HTTP 对端。记忆状态场景使用合成向量，适合证明一致性，不能证明真实语言理解或公开数据集上的质量。

当前结果、保留的失败与限制见[验证入口](../docs/operations/verification.md)。
