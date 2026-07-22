# Kirakira Agent 简历与面试讲解稿

## 1. 写作原则

- 简历写自己解决的问题和做出的工程取舍，不写“参考某某项目实现”。
- 只写代码中已经存在、自己能够沿调用链讲清楚的能力。
- LangSmith、BM25、HyDE、FastAPI、PostgreSQL、pgvector、Redis 和压测在落地前只能写进后续规划；RRF 已落地并有回归，必须和尚未实现的 BM25/HyDE 分开描述。
- 每条项目亮点都要能回答：为什么做、调用链是什么、遇到什么 Bug、怎么测试、有什么 tradeoff。
- 不用“大幅提升”“显著降低”等无法证明的词；有 baseline 后再写具体百分比。

## 2. 当前版本简历写法

### 2.1 推荐完整版

**Kirakira Agent｜多渠道、可扩展的长期对话 Agent Runtime**

`Python / asyncio / Function Calling / SSE / MCP / SQLite FTS5 / Telegram / OneBot`

从最小 Function Calling MVP 逐步搭建可长期运行的 Agent Runtime，完成模型推理、工具执行、会话状态、长期记忆和多渠道接入的端到端闭环；支持 Web、Telegram、QQ/OneBot 与 CLI，已使用 DeepSeek 在线验证真实多轮工具调用和后台记忆抽取。

- **Agent Loop 与并发编排：** 设计 `MessageBus -> AgentLoop -> PassiveTurnPipeline -> Reasoner` 执行链，同 session 串行避免历史交叉写入、跨 session 并行提升吞吐；实现 SSE 增量事件、同 chat 回复保序、turn 中断及 partial tool-chain 持久化，并统一管理 Channel、MCP、后台任务和 memory worker 的优雅关机。
- **Function Calling 与工具系统：** 抽象 ToolRegistry/ToolExecutor，统一工具 schema 暴露、参数校验、异步执行、超时和错误语义；支持 pre/post/error Hook、deferred tool discovery 与 session LRU，接入 stdio MCP 远端工具，并对文件路径、Shell 进程组及 `web_fetch` SSRF 建立执行边界。
- **热重载与代际快照：** 将 MCP 从命令式注册重构为声明式热重载（内容 revision 驱动、整批候选语义、失败保持旧代际服务）；引入 RuntimeSnapshot + 每 turn 租约，解决"turn 执行期间工具被换掉"的竞态——换代只切 current 指针，在途 turn 继续使用其锁定的代际，旧 MCP 进程等租约计数归零后才断开。
- **会话与长期记忆：** 使用原子 JSON 保存 session 和完整 reasoning/tool history，以 SQLite FTS5 建立可重建的消息索引；设计 typed memory、source evidence、强化/遗忘和 session 删除撤销，结合中文词法与可选 embedding 混合召回，并在回复后异步 consolidation，避免记忆抽取阻塞用户响应。
- **上下文治理：** 将 Prompt 拆为具名 block，分离稳定 system 与逐轮 Context Frame；统一预算 system/messages/tool schema/image，并在 Provider 请求前预检，按语义 section 逐级重渲染；持久化 retry plan、section/cache、模型实际 usage 和下一轮 baseline，避免长对话静默丢历史。
- **扩展与可靠性：** 实现插件程序化能力声明、config/KV、生命周期模块、工具 Hook、inline/background subagent 和显式调度；针对 Web 并发串回复、DeepSeek reasoning 回放、session 文件名碰撞、MCP/插件半加载、重定向 SSRF 和后台任务关机竞态补充回归测试；建立 fail-loud 边界（向量检索可降级、向量写入必须报错）。

### 2.2 一页简历压缩版

**Kirakira Agent｜长期对话 Agent Runtime**

- 从 Function Calling MVP 搭建 `MessageBus -> AgentLoop -> ToolExecutor` 异步执行链，实现同会话串行、跨会话并发、SSE 流式事件、turn 中断和完整工具链持久化。
- 设计 ToolRegistry/ToolExecutor，支持 schema 校验、Hook、延迟工具发现、声明式 MCP 热重载及文件/Shell/网络安全边界，控制工具 schema 膨胀并在产生副作用前拦截错误参数。
- 构建 Session + 长期记忆系统，使用 JSON/SQLite FTS5 管理对话与历史回源，结合 lexical/vector 多路召回、RRF、热度半衰、source evidence、遗忘撤销和异步 consolidation 支持长期对话。
- 实现具名 PromptBlock、Context Frame、Provider 预算预检和可解释降级 trace；接入 Web、Telegram、QQ/OneBot、插件与后台子 Agent，并完成 DeepSeek 在线工具循环、记忆抽取与 context usage 验证。

### 2.3 为什么这样写

这四条分别对应面试可以展开的四条真实主线：

1. Runtime 编排和并发。
2. 工具系统。
3. Session/Memory/Retrieval。
4. 扩展性、Bug 和工程质量。

不会出现一条简历塞入十几个名词，却无法讲清任何一条调用链的问题。

## 2.5 差异化主线：三条链路（面试主打）

> 上一次面试被吊"缺乏与市面项目的差异化"。根因很直接：只讲了**被动回复**——这条链路
> 和市面所有 chatbot/agent 一模一样。真正的差异点是另外两条链路，现在都做出来了，
> 面试时**先讲这两条**。

**一句话定位**：市面多数 agent 只有"你问它答"一条链路；Kirakira 有三条并列链路，
后两条是它区别于普通 chatbot 的地方——它**会自己找你**，还能**在空闲时自主干活**。

| 链路 | 触发方 | 行为定义在哪 | 差异点 |
| --- | --- | --- | --- |
| 被动回复 | 用户消息 | 代码（agent loop） | 与市面一致，是基座 |
| 主动推送 | 后台定时轮询 | 代码（system prompt） | **电量模型自适应节流 + 三通道语义** |
| Drift | 主动链路空转时 | **你写的 SKILL.md** | **行为可编辑、不改代码、跨轮连续性** |

### 可加进简历的一条

**主动推送与自治后台链路：** 在被动 agent loop 之外实现两条自治链路。主动推送用多时间尺度
指数衰减"电量"与近期对话活跃度调节检查频率（长期沉默或近期语境丰富时加快检查），每 tick 拉取
`alert`/`content`/`context` 三通道数据，经 LLM 兴趣判断决定推送或跳过，事件按稳定 id 去重、
按冷却窗口节流；Drift 在主动链路本轮未产生推送时复用同步 Agent 与默认工具集跑后台任务，任务行为由
用户编写的 `SKILL.md` 定义而非硬编码，并在 SQLite 中保存跨轮连续性与最小间隔门控。

### 高频追问预案

**问：主动推送会不会一直骚扰用户？频率怎么定？**
不是固定间隔。用一个多时间尺度指数衰减的"电量"值 `E(t)`（30min/240min/48h 三个时间常数）
刻画"距上次互动多久"，合成一个 `base_score`；分数越高（久没聊，或刚聊得很热）→ 下次 tick
间隔越短。刚聊完时单看 `D_energy` 会降低，但若近期对话很丰富，`D_recent` 又会把
`base_score` 拉高；半天没动静时 E 衰减、`D_energy` 上升，间隔缩短。
再加随机抖动避免整点齐发。纯数学、可单测，不额外打模型。

**问：三个通道有什么区别？**
`alert` 优先发送并由模型自然化表达（会议提醒这种）；`content` 要经过一次 LLM 兴趣判断，宁缺毋滥，不确定就
skip；`context`（睡眠/在线状态）从不单独触发推送，只作为背景注入判断 prompt。判断顺序是
alert 优先 → content 兴趣判断 → 都没推就进 Drift。

**问：怎么保证同一条新闻不被反复推送？**
事件有稳定身份 `<source>:<event_id>`，入库用 `INSERT OR IGNORE` 去重；只有被 LLM 真正引用
（cited）的事件才 consume + ACK，没引用的留在未读队列。推送后记 `last_push_at`，冷却窗口内
抑制 content 刷屏（alert 不受此限）。

**问：数据从哪来？是写死的吗？**
不是。定义了一个 `ProactiveSource` 协议（`fetch`/`ack` + 声明通道），链路只认 source/channel/事件，
不硬编码数据来源。MVP 内置一个从 `proactive/inbox/*.jsonl` 读事件的文件源做演示；要接 RSS/日历/
健康数据，实现同一个协议（fetch 调 MCP 工具、ack 回源）即可无缝替换。

**问：Drift 和主动推送有什么本质区别？**
主动推送的行为是**代码里写死的 system prompt**；Drift 的行为是**用户写在 SKILL.md 里的分步指南**，
可编辑、可增删，不动代码。一轮 Drift 就是一次 agent run：SKILL.md 当 system prompt，注入一份
Briefing（记忆 + 近期上下文 + 本 skill 前情），复用同步 Agent 与默认工具集一步步执行，最后调
`finish_drift` 收尾。跨轮连续性（续跑断点、倾向）存在 SQLite 里，下轮通过 Briefing 注入。

**问：这块的 tradeoff / 有没有偷工？（要诚实）**
明确说是 **MVP**：先做到两条链路能执行、留好拓展位，参考项目的重型机制（phase-graph kernel、
snapshot 热重载、插件化数据源运行时、语义兴趣向量、hazard 概率穿线）刻意暂缓。比如判断层
因为 `ModelClient.complete` 不支持 forced tool call，改成让模型产出严格 JSON 再解析——这是
provider 能力受限下的有意实现差异，不是没想到。这样讲比假装全量复刻更经得起追问。

## 3. 当前不能写成“已完成”的内容

- FastAPI 分层 API 和 Worker 消费链路。
- PostgreSQL 多租户表结构。
- Redis 分布式锁、队列和限流。
- pgvector、BM25、HyDE、query rewrite（RRF 已完成）。
- LangSmith trace/eval 回归。
- 内容审查和后台管理系统。
- 100–200 用户并发压测。

这些都是合理的下一阶段，但在落地和测试前写入简历，面试官只要追问一次真实调用链就会暴露。

## 4. 高频追问：工具调用到底怎么执行

### 问：模型是不是直接执行工具？

不是。模型只返回结构化意图：工具名和参数。Reasoner 解析 ToolCall 后交给 ToolExecutor；ToolExecutor 先做 Hook，再由 ToolRegistry 找到 handler 执行，结果标准化为 ToolResult 回填模型历史。

```text
LLM ToolCall
  -> Reasoner visibility guard
  -> ToolExecutor
      -> pre hook
      -> ToolRegistry.execute_async
          -> schema validation（最终参数）
      -> local handler / subprocess / HTTP API / MCP
      -> post hook
  -> ToolResult
  -> append role=tool message
  -> next LLM iteration
```

### 问：web_search 这种需要调用 API 的工具怎么传上下文？

工具 handler 接收模型 schema 中定义的业务参数，例如 query/limit。session_key、channel、chat_id 不是让模型生成，而是 Runtime 在 turn 开始时通过 ContextVar 绑定，避免模型伪造路由信息。工具执行结果以文本或结构化 JSON 返回 ToolResult；Reasoner 把它作为 `role=tool` 消息送入下一轮模型。

如果工具需要凭证，凭证来自服务端配置或插件 data/config，不进入模型上下文，也不由模型参数传入。

### 问：参数错了怎么办？

Registry 在 handler 前校验 required/type/enum。校验失败不会产生副作用，而是将错误作为 ToolResult 返回模型，让模型有机会修正参数。重复相同错误调用达到阈值后，Loop Guard 会停止继续执行。

### 问：为什么还需要 ToolExecutor，Registry 不能直接执行吗？

Registry 负责目录、schema 和 dispatch；Executor 负责一次调用的策略管线，包括 Hook、deny、timeout、状态和错误语义。拆分后插件和安全策略不需要侵入每个工具 handler。

### 问：工具多了怎么避免 Prompt 膨胀？

核心工具 always-on，其他工具 deferred。模型先调用 `tool_search`，选中后才在下一轮暴露 schema；session 只保留有限 LRU。MCP 远端工具也进入同一套 deferred registry。

### 问：长上下文怎么管理，为什么不是直接截断？

Prompt 先拆成稳定 system block 与动态 Context Frame，预算同时覆盖消息、工具 schema 和图片。
超限时每次重新经过 prompt hooks，并按 `skills catalog → recent context → long-term memory →
retrieved memory → history` 降级；历史只从 user boundary 切。Provider 在网络请求前做最终预检，
每次 attempt、section/cache、实际 token usage 和下一轮 baseline 都写进 session trace。未归档历史过多时
先强制 consolidation，游标不前进就明确阻断，不能静默遗忘。

### 问：MCP 工具为什么不单独执行？

MCP 只是远端工具协议。`tools/list` 转成 ToolSpec，`tools/call` 包成 handler，再注册进统一 Registry，这样 MCP 工具同样经过可见性、Hook、schema、timeout 和 lifecycle。

## 5. 高频追问：记忆系统

### 问：为什么不直接把全部历史塞给模型？

全部历史会线性增长，破坏上下文预算和 Prompt Cache，也无法区分稳定偏好与一次性状态。因此近期窗口保留原始对话，长期内容通过 consolidation 抽取为结构化记忆，需要时再召回。

### 问：记忆怎么写入？

用户明确要求记住时可以调用 `memorize`；普通对话在回复后由后台 worker 按窗口抽取 identity/preference/procedure/event。写入带 source_ref，exact duplicate 做强化，同源重放保持幂等。

### 问：为什么 consolidation 放后台？

记忆抽取需要额外 LLM 请求，放在主回复前会增加用户等待。当前先 commit 和回复，再后台抽取；同 session 下一轮开始前等待上一轮收口，兼顾响应延迟和读写一致性。

### 问：用户删除对话怎么办？

记忆保留 source_ref。Session 删除触发 callback，将该 session 产生的记忆置为 forgotten，并同步重写 MEMORY.md 托管区。

### 问：当前检索是什么？

必须如实回答：当前用中文 bigram/英文 token/substring 形成 lexical lane，可选 embedding cosine
形成 vector lane；两路先各自准入和排序，再用 RRF（k=60，lexical 权重 0.5）融合，并叠加
reinforcement + 14 天半衰的 hotness，最后受 type/time 与 1200 字符注入预算约束。还没有 BM25、
HyDE 和 query rewrite。

### 问：为什么用 RRF？

cosine 与 keyword overlap 的原始分数尺度不同，直接加权没有统一含义。RRF 只依赖各 lane 内部名次，
不要求跨 lane 分数可比；同时每条 lane 先做准入，避免无关记录因为“排在末尾”仍被塞满 limit。
当前实现已有代码级回归；是否增加 BM25、query rewrite 或 HyDE 仍必须通过固定数据集评测。

## 6. 高频追问：并发和一致性

### 问：为什么同 session 串行？

同 session 的两条消息若同时读取旧历史并写回，会导致顺序错误、记忆重复和回复上下文不一致。AgentLoop 为每个 session 使用 lock；不同 session 使用不同 task 并发。

### 问：Web 同 session 并发请求怎么对应回复？

只用 chat id 不够。每个请求生成 correlation id，沿 Inbound metadata、Pipeline、Outbound metadata 传播，Web pending future 只接受匹配 id 的回复。

### 问：当前能支持 100–200 用户吗？

当前是单进程、文件 canonical store，适合个人或小规模运行，没有做 100–200 用户压测。要达到该目标需要 API/Worker 解耦、PostgreSQL 多租户存储、Redis/数据库 session lock、幂等 turn、连接池、限流和负载测试。不要在当前版本夸大。

## 7. 可讲的真实 Bug 闭环

面试时按“现象 -> 根因 -> 修复 -> 回归”讲，不要只说修了很多 Bug。

### Bug 1：同会话并发串回复

- 现象：两个 Web 请求可能拿到对方回复。
- 根因：pending response 只按 chat id 路由。
- 修复：端到端传播 client request id。
- 回归：同 session 并发请求测试，故意让后发请求先完成。

### Bug 2：DeepSeek 多轮工具调用失败

- 现象：第一轮工具执行正常，后续请求协议错误或推理断裂。
- 根因：重建 history 时丢失 tool-call assistant 的 `reasoning_content`。
- 修复：每个 tool group 保存 reasoning 并原样回放。
- 回归：构造 reasoning + tool call + tool result 历史并检查序列化。

### Bug 3：关机留下后台任务

- 现象：主 Loop 停止后 subagent 仍完成并向 inbound 回注，消息无人消费。
- 根因：shutdown 顺序错误。
- 修复：先取消派生任务，再停止 Loop 和 Bus；Shell 同样回收进程组。
- 回归：启动阻塞后台任务后执行 shutdown，断言无 pending task/process。

### Bug 4：SSRF 重定向绕过

- 现象：初始公网 URL 可以重定向到本地服务。
- 根因：只校验第一跳 URL。
- 修复：每次 redirect 重新 DNS/IP 校验。
- 回归：本地 HTTP server 返回 redirect，默认必须拒绝。

### Bug 5：删除对话后记忆污染

- 现象：Session 已删除，但长期偏好仍被召回。
- 根因：记忆没有 source 生命周期。
- 修复：source_ref + delete callback + managed Markdown rewrite。
- 回归：删除一个 session，只撤销其来源记忆，不影响其他来源。

## 8. 下一版完成后怎么升级简历

### 8.1 LangSmith/eval 落地后可替换的一条

**评测与回归：** 基于 LangSmith 构建工具选择、参数生成、记忆写入/召回和冲突处理数据集，记录 commit、模型、Prompt 与 schema 版本；使用规则评测和 LLM Judge 对比 baseline/candidate，以工具成功率、记忆命中率、无关注入率、token 与 p95 latency 设置发布门禁，并支持回滚到最后通过基线的配置。

只有真实接入 trace、dataset、runner 和 CI gate 后再使用这条。

### 8.2 RAG 升级落地后可替换的一条

**混合记忆检索：** 将长期记忆检索拆分为 vector、BM25/keyword 与 auxiliary query 多路召回，使用 RRF 融合不同分数空间，并结合 type/time/source/confidence rerank；通过固定记忆数据集评估 query rewrite/HyDE 对召回率和噪声注入的影响。

只有实现 BM25、auxiliary query/rerank 并完成固定数据集评测后再使用这条；其中 RRF 已经完成，
不能把整条未来方案笼统写成“已实现”。

### 8.3 后端化完成后可增加的两条

**服务与异步执行：** 使用 FastAPI 提供消息提交、turn 状态和 SSE 接口，将回复状态抽象为 pending/processing/done/failed；通过 Worker 异步消费 turn，解决长模型调用阻塞 API 请求的问题。

**多租户存储与并发：** 使用 PostgreSQL 持久化 user/session/message/turn/memory，结合 Redis 或数据库锁保证同会话串行、跨会话并发；通过幂等 turn id、行级用户作用域和压测验证 100–200 用户场景。

必须有真实 migration、worker、隔离测试和压测报告后再写。

## 9. 项目介绍口述版

“这个项目最开始只是一个 Function Calling MVP，模型返回工具名和参数，我执行后把结果回填。工具一多，我先解决参数校验、异常、超时和上下文串线，把执行拆成 ToolRegistry 和 ToolExecutor，再加入 Hook、延迟工具发现和 MCP。之后为了支持长期对话，我把原始 Session 和长期记忆拆开：Session 保存完整 tool history，长期记忆在回复后异步抽取，并保留 source 以支持遗忘和撤销；检索用 lexical/vector 多路召回与 RRF 融合。长上下文再拆为具名 PromptBlock，并用 Provider 预检、语义降级和 trace 避免静默截断。多渠道接入后，我用 MessageBus 和 session lock 保证同会话串行、跨 session 并发，也处理了 streaming、取消和优雅关机。在被动回复之外，我又加了两条自治链路：主动推送用电量衰减和近期活跃度调节检查频率，拉 alert/content/context 三路数据、经 LLM 兴趣判断决定要不要主动发消息；本轮没有产生推送时尝试进入 Drift，复用同步 Agent 与默认工具集去跑用户写在 SKILL.md 里的后台任务。这两条才是它区别于普通 chatbot 的地方——它会自己找你、也会自己找事做。下一步是用 durable outbox 补齐跨崩溃可靠提交、接入真实插件数据源，再用 LangSmith/eval 建立工具、记忆和主动决策的回归基线。”

这段口述能自然引出工具系统、记忆、并发、Bug、差异化的两条链路和下一版评测。差异化部分（主动推送 + Drift）应当**最先讲**，直接回应"和市面项目有什么不同"。
