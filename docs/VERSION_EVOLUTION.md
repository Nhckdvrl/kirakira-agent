# Kirakira Agent：从 MVP 到完整 Agent Runtime

## 1. 为什么从 MVP 讲起

这个项目最有价值的部分，不是最终堆出了多少模块，而是一个最小 Agent 如何在真实问题推动下逐步工程化：

```text
MVP：模型能调用一个工具
  ↓
工具执行可靠：参数、错误、超时、上下文都可控
  ↓
工具规模扩大：动态发现、延迟加载、MCP、Hook
  ↓
对话可持续：Session、历史回放、长期记忆
  ↓
系统可运行：MessageBus、并发、Channel、Streaming
  ↓
系统可扩展：Plugin、Subagent、Schedule
  ↓
当前：完整被动式 Agent Runtime
  ↓
下一版：评测驱动的工具编排与回归体系
  ↓
后续：100–200 用户、多租户和后台管理
```

下面的“版本”是工程演进阶段，不是为了包装而虚构的发布标签。每一阶段都回答四个问题：最小目标是什么、为什么要加下一层、实际解决了什么、还留下什么问题。

## 2. MVP：先让模型正确调用工具

### 2.1 最小可行目标

MVP 只验证一件事：模型能否根据用户请求选择工具，Harness 能否执行工具，并将结果以正确协议送回模型。

```text
User Message
    ↓
LLM 返回 ToolCall(name, arguments)
    ↓
ToolRegistry 查找并执行 Handler
    ↓
ToolResult 回填 messages
    ↓
LLM 生成最终回复
```

最初只需要：

- 一个 OpenAI-compatible 模型客户端。
- 一个 `ToolSpec`，描述 name、description 和 JSON schema。
- 一个 `ToolRegistry`，保存 spec 与 handler。
- 一个循环：模型回复工具调用就执行，否则结束。
- `bash`、`read_file`、`write_file` 等少数基础工具。
- CLI 输入输出。

### 2.2 MVP 为什么是“可用”而不是“完整”

它已经能完成“读取文件并解释”“运行命令并总结”这类任务，因此验证了产品核心假设。但它只能服务一个本地会话，错误处理和状态管理非常薄弱。

### 2.3 MVP 暴露的问题

- 模型可能传错参数、漏参数或调用不存在的工具。
- 工具异常可能直接打断整个循环。
- Shell 可能超时，子进程可能残留。
- Tool Result 太长会撑爆上下文。
- 多个工具越来越难全部暴露给模型。
- 没有 trace，出了问题只能猜是模型、工具还是协议错了。

因此第一轮工程化不应马上做复杂记忆，而应先把工具执行链路跑稳。

## 3. 第一轮工程化：让工具执行不出错

### 3.1 从函数调用升级为执行管线

工具真实链路演进为：

```text
Reasoner
  -> ToolExecutor.execute(request, ToolRegistry.execute_async)
      -> pre hooks
      -> registry dispatch
      -> schema validation（校验 Hook 改写后的最终参数）
      -> handler / API / subprocess
      -> post success or post error hooks
      -> normalized ToolResult
  -> append tool message
  -> next LLM iteration
```

### 3.2 ToolRegistry 的职责

ToolRegistry 不只是字典，而是统一工具目录和执行边界：

- 保存工具 schema 与 handler。
- 向模型暴露可见工具定义。
- 执行前校验 required、type、enum。
- 支持同步和异步 handler。
- 同步阻塞工具通过 worker thread 执行，避免卡住 event loop。
- 用 `ContextVar` 保存当前 session/channel/chat，避免并发串线。
- 统一未知工具、参数错误和 handler 异常为 ToolResult。

### 3.3 ToolExecutor 的职责

ToolExecutor 将“工具能不能执行”与“工具怎么执行”分离：

- pre hook 可以改参数或拒绝执行。
- pre hook 异常 fail-closed，避免安全模块失效后继续执行。
- post hook 记录结果或追加上下文。
- timeout 和取消统一变成错误结果。
- lifecycle 事件记录工具开始、结束、状态和耗时扩展点。

### 3.4 文件、Shell 和网络工具的边缘治理

- 文件路径必须位于 workspace 内。
- 写文件使用临时文件 + replace，避免半写入。
- edit 在多个匹配时拒绝默认替换，防止误改。
- 文本读取检查二进制 NUL。
- Shell 使用独立进程组，timeout/cancel/shutdown 都清理子进程树。
- 长任务可转后台，通过 `task_output` 轮询、`task_stop` 终止。
- `web_fetch` 检查 DNS/IP 和每一次 redirect，阻止 SSRF 访问私网。
- 网络响应限制类型和大小。

### 3.5 这一阶段解决了什么

Agent 不再只是“模型说调用就调用”，而是在 schema、权限、Hook、超时和标准化错误之内执行。工具问题可以被定位到参数校验、pre hook、handler、post hook或模型下一轮决策。

## 4. 第二轮工程化：工具多了以后怎么管理

### 4.1 为什么不能把所有 schema 永远塞给模型

工具数量增长会带来三个问题：

- schema 占用上下文，增加 token 与延迟。
- 相似工具变多，模型更容易误选。
- MCP 或插件工具运行时才出现，静态列表无法覆盖。

### 4.2 延迟加载与动态发现

当前实现将工具分为 always-on 和 deferred：

1. 首次请求只暴露核心工具和 `tool_search`。
2. 模型调用 `tool_search` 检索目录。
3. `select:<tool_name>` 解锁目标工具。
4. 下一轮模型请求才注入该工具 schema。
5. 每个 session 使用 5 项 LRU，避免已解锁工具无限增长。
6. 模型绕过搜索直接调用隐藏工具时，Reasoner 拒绝执行并返回选择提示。

### 4.3 MCP 动态注册

MCP 接入不是另写一套执行器，而是适配到统一 Registry：

```text
mcp_add
  -> 启动 stdio MCP server
  -> initialize
  -> tools/list
  -> 将远端 schema 转成 ToolSpec
  -> 注册 McpToolWrapper 到 ToolRegistry（deferred）

模型调用远端工具
  -> ToolExecutor
  -> ToolRegistry
  -> McpToolWrapper
  -> tools/call JSON-RPC
  -> ToolResult
```

这样本地工具、插件工具和 MCP 工具共享 schema 校验、Hook、生命周期、错误语义和可见性策略。

### 4.4 插件与 Hook

插件可以：

- 注册新工具。
- 通过 pre hook 阻断或改写工具。
- 在 turn phase 注入上下文或提前返回。
- 声明 skills 和 MCP servers。
- 使用独立配置、KV 和 data dir。

插件加载失败会撤销已经注册的资源，单个坏插件不会阻止 Runtime 启动。

### 4.5 下一步编排空间

当前编排核心仍由模型自由选择工具，工程控制主要是可见性、权限和循环保护。下一版应进一步加入：

- capability policy，而不是维护 disabled tool 名单。
- read/write/network/process/admin 风险分级。
- 对高风险工具增加 approval 或 policy gate。
- 记录工具选择、参数修复、重试、fallback 和终止原因。
- 建立工具路由评测集，判断模型是否选对工具、参数是否正确。

## 5. 第三轮工程化：从单轮工具 Agent 到长期对话

### 5.1 Session 先解决短期状态

Session 保存用户消息、助手回复、reasoning、tool calls 和 tool results。重建模型历史时，必须展开成协议正确的 assistant/tool 消息序列，并从 user boundary 开始，避免孤立 tool message。

当前 Session 使用：

- JSON 作为 canonical store。
- 临时文件 + `os.replace` 原子保存。
- 可读 key + hash 防止文件名清洗碰撞。
- SQLite FTS5 trigram 作为可重建的消息搜索索引。
- `search_messages` 返回 source_ref，`fetch_messages` 回源 JSON。

### 5.2 为什么短期历史不等于长期记忆

完整历史直接塞入 Prompt 会持续增长，也无法区分稳定偏好和一次性内容。长期记忆需要独立处理：

- 哪些内容值得保存。
- 内容属于身份、偏好、流程还是事件。
- 如何去重、强化、更新和遗忘。
- 回答当前问题时是否需要召回。
- 召回结果如何追溯到原会话。

### 5.3 当前记忆写入链路

```text
Turn committed
  -> 同步写 RECENT_CONTEXT / HISTORY
  -> 回复先返回用户
  -> 后台 consolidation worker
  -> LLM 从窗口对话中抽取结构化 memories/history
  -> exact dedup + reinforcement
  -> 写 items.json 和 MEMORY.md 托管区
  -> 更新 last_consolidated
```

同 session 下一轮开始前会等待上一轮 consolidation 收口，避免边写边读。Session 删除时，带 source_ref 的记忆会被撤销，避免“对话删了，事实还在”。

### 5.4 当前检索真实实现

当前不是截图示例中的 BM25 + RRF + HyDE。现在真实实现是：

- 中文 bigram 和英文 token 词法匹配。
- substring 加权。
- 可选 embedding cosine 语义召回。
- 配置 embedding 后使用 `0.75 * semantic + 0.25 * lexical` 融合原始分数。
- reinforcement 提供少量加权。
- 支持 memory type、since、until 过滤。

这个方案代码少、依赖低，适合当前单机版本，但原始分数直接加权存在尺度不可比问题，也缺少 query rewrite、语义去重和矛盾处理。

### 5.5 为什么下一版考虑多路召回 + RRF

更稳妥的下一步是：

```text
原始 query
  -> query rewrite / auxiliary queries
  -> vector lane：原 query + 改写 query
  -> keyword lane：BM25/FTS 精确词
  -> 各 lane 独立排序
  -> RRF 按 rank 融合
  -> evidence/type/time/confidence rerank
```

RRF 的价值是避免直接比较 cosine 与 BM25 的原始分数。向量更适合口语化和同义改写；keyword 更适合变量名、命令、路径、错误码和精确实体。是否加入 HyDE 必须经过评测，不应仅因为方案听起来高级就默认开启。

## 6. 第四轮工程化：多渠道、并发与异步链路

### 6.1 为什么引入 MessageBus

CLI、Web、Telegram 和 QQ 都需要相同 Agent 行为。如果 Channel 直接调用模型，session、记忆、错误和流式逻辑会复制多份。

统一链路为：

```text
Channel -> InboundMessage -> MessageBus -> AgentLoop
        -> PassiveTurnPipeline -> OutboundMessage -> Channel
```

### 6.2 并发模型

- 同 session 串行，避免历史交叉写入。
- 不同 session 并行，避免慢用户阻塞其他会话。
- 同 chat outbound ticket 保序。
- 不同 chat 并发发送。
- Web request 使用 correlation id，防止同 session 并发请求拿错回复。
- `/stop` 取消 active turn，并保存 partial reply/thinking/tool chain。

### 6.3 Streaming

模型 SSE 被解析为正文、reasoning 和 fragmented tool calls。Stream delta 通过 lifecycle event 发给 Channel：Telegram 先发送占位消息再 edit，Web 通过事件接口获取进度，最终回复仍走统一 OutboundMessage。

### 6.4 资源生命周期

正常关闭需要按顺序处理 subagent、AgentLoop、scheduler、bus drain、Channel、后台 Shell、插件、MCP、memory worker、EventBus 和 Session index。只停止主循环会留下子进程、后台任务或未完成队列。

## 7. 第五轮工程化：Subagent、Schedule 与可扩展 Runtime

### 7.1 Subagent

- inline 模式阻塞当前 turn，结果直接作为 ToolResult。
- background 模式立即返回 task id，完成后回注原 session。
- 独立 session 隔离上下文。
- research/scripting/general profile 控制工具权限。
- 禁止递归 spawn、发消息、改 MCP/插件和创建 schedule。
- 最大并发 3，支持 list/cancel。

### 7.2 Schedule

Schedule 只执行用户明确创建的定时消息，不包含自主决策。任务持久化 fire time、interval 和 status，到期后通过 MessageBus 发往原 Channel。

### 7.3 当前完整版本

截至当前，项目已经具备：

- Web、Telegram、QQ/OneBot、CLI。
- session-aware AgentLoop 和 streaming tool loop。
- ToolRegistry、ToolExecutor、Hook、deferred discovery 和 MCP。
- Session、FTS 消息搜索、长期记忆和后台 consolidation。
- Plugin、Subagent、Schedule 和 graceful shutdown。
- 83 项自动化测试。
- DeepSeek 在线普通响应、真实工具调用和记忆抽取验证。

这已经是完整被动式 Agent Runtime，但还不是面向多租户和 SLA 的生产平台。

## 8. 实际遇到并修复的 Bug

### 8.1 Web 并发请求串回复

问题：同一 session 同时发送两个 HTTP 请求时，只按 chat id 等待 outbound，后返回的请求可能拿到前一个回复。

修复：每个请求生成 `client_request_id`，Pipeline 将其传播到 Outbound metadata，Web 只解析匹配 correlation id 的 future。

验证：增加同 session 并发请求集成测试。

### 8.2 Outbound 队列无法 graceful drain

问题：dispatch 后未在所有路径调用 `task_done()`，关机等待 queue join 可能永久卡住。

修复：将完成标记放进 dispatch task 的 `finally`，并测试同 chat 顺序和跨 chat 并发。

### 8.3 DeepSeek 工具历史协议错误

问题：DeepSeek thinking 模式下，包含工具调用的 assistant 消息需要回传对应 `reasoning_content`；丢失后续轮次可能被 API 拒绝或推理断裂。

修复：每个 tool-chain group 持久化 reasoning，Session history reconstruction 原样恢复。

### 8.4 Session 文件名碰撞

问题：只替换特殊字符时，`a:b` 和 `a/b` 可能落到同一文件。

修复：文件名使用可读前缀 + 原 key SHA-256 摘要，并兼容迁移旧文件。

### 8.5 后台子任务在主循环关闭后回注

问题：关机先停止 AgentLoop，再等待 subagent 完成，会把 completion 写入无人消费的 inbound queue。

修复：运行时关机先取消后台子任务，再关闭 Loop；用户 cancel 与 shutdown cancel 使用不同语义。

### 8.6 SSRF 重定向绕过

问题：只校验初始 URL 时，公网地址可以 302 到 localhost 或私网。

修复：自定义 redirect 处理，每一跳重新解析 DNS/IP，并限制响应大小和内容类型。

### 8.7 插件初始化半成功

问题：插件先注册工具后 initialize 报错，会留下半加载工具。

修复：记录插件注册资源，失败时统一 rollback；坏插件错误隔离，不阻塞后续插件。

### 8.8 删除 Session 后长期记忆仍存在

问题：用户删除对话，但 consolidation 产生的记忆继续参与召回。

修复：Memory 记录 source_ref，Session delete callback 将对应记录标记 forgotten 并重写托管 Markdown。

## 9. 下一版：工具编排 + LangSmith 评测回归

下一版最重要的不是继续加普通工具，而是证明“工具选择更准、参数更稳、改动不会让旧场景退化”。

### 9.1 Trace 接入

将以下节点记录到 LangSmith 或兼容 trace runner：

- turn/session/channel/model。
- PromptRender 后的输入摘要和可见工具名。
- 每次模型返回的 tool selection。
- schema validation、pre hook、handler、post hook。
- tool latency、error、retry、denied reason。
- memory query、候选、排名和最终注入。
- final answer、token、latency 和终止原因。

敏感字段必须脱敏，API key、完整私密附件和高风险工具参数不能原样上传。

### 9.2 工具系统评测集

至少覆盖：

- 正确选工具。
- 不需要工具时不调用。
- 相似工具消歧。
- 缺参数、错类型、错 enum。
- deferred tool 是否先 search 再调用。
- MCP 工具断连与 fallback。
- 重复工具调用是否及时停止。
- 高风险工具是否被 Hook 拦截。
- 工具成功后最终回复是否忠于结果。

### 9.3 记忆评测集

- 应记住的稳定偏好是否写入。
- 短期状态是否不会被误存为长期事实。
- 历史事实与当前事实冲突时是否正确处理。
- 删除 session 后 source memory 是否失效。
- 口语改写是否能语义召回。
- 变量名、路径、错误码是否能关键词召回。
- 无关问题是否不会注入噪声记忆。
- consolidation 重放是否幂等。

### 9.4 基线、回归与回滚

```text
固定 Dataset
  -> baseline commit + prompt/model/config
  -> candidate commit
  -> deterministic/code evaluators
  -> LLM judge（只用于难以规则判断的质量项）
  -> 对比准确率、工具成功率、记忆命中、token、p95 latency
  -> 未过阈值则阻止发布
```

每次运行记录 commit SHA、模型、Prompt 版本、工具 schema 版本、memory strategy 和 evaluator 版本。回滚不是“凭感觉改回 Prompt”，而是回到最后一个通过 gate 的版本和配置。

### 9.5 下一版验收指标

- 工具选择准确率。
- 工具参数一次通过率。
- Tool Loop 完成率和平均迭代数。
- 重复调用拦截率。
- 记忆写入 precision、召回 recall、无关注入率。
- token、TTFT、turn p50/p95。
- Bug fixture 全量回归通过。

具体阈值应在第一批真实数据跑完后确定，不能在没有 baseline 时随意编百分比。

## 10. 再下一版：100–200 用户的后端化

这一层目前是设计方向，不应写进当前简历的“已完成”部分。

### 10.1 服务拆分建议

```text
FastAPI / WebSocket / SSE Gateway
        ↓
Auth + Rate Limit + Moderation
        ↓
PostgreSQL：user/session/message/turn/memory/job
        ↓
Redis：短期状态、分布式锁、队列/stream、限流
        ↓
Worker：Agent Turn / Memory Consolidation / Embedding
        ↓
LLM、Tool/MCP、pgvector、对象存储
```

### 10.2 多用户隔离

- 所有业务表带 `tenant_id/user_id`。
- Repository 查询默认注入用户作用域。
- Session lock 从进程内 Lock 升级为数据库或 Redis 锁。
- Worker 消费使用幂等 turn id。
- PostgreSQL 可增加 Row Level Security 作为第二层隔离。
- Tool workspace、插件数据和附件路径按用户隔离。

### 10.3 数据模型

- `users`：身份、状态、配额。
- `sessions`：channel、owner、metadata、version。
- `messages`：role、content、media、seq。
- `turns`：pending/processing/done/failed、trace、token、error。
- `tool_calls`：name、arguments、status、latency、result reference。
- `memories`：type、summary、embedding、source、status、confidence。
- `jobs`：schedule/subagent/consolidation 的统一状态机。

### 10.4 Worker 与并发

- API 只负责提交 turn 和返回 turn id。
- Worker 异步执行 AgentLoop。
- `FOR UPDATE SKIP LOCKED` 或消息队列避免重复消费。
- 同 session 使用 advisory lock/Redis lock 保序。
- 结果通过 SSE/WebSocket 或轮询回传。
- Tool call 和 memory write 使用幂等 key。

### 10.5 内容审查

- 入站文本、附件和出站回复分别审查。
- 高风险工具调用走 policy engine，而不是只审查最终文本。
- 审查结果、规则版本和处置写入 audit log。
- 对误杀提供人工复核和申诉状态。
- 管理后台展示用户、turn、tool call、memory、moderation 和 trace。

### 10.6 什么时候可以写进简历

至少完成：

- PostgreSQL 多租户数据模型与 migration。
- API/Worker 分离和同 session 并发控制。
- 100–200 虚拟用户压测报告。
- 内容审查链路与后台查询。
- 故障恢复和幂等测试。

完成后再把简历前两条升级为“FastAPI + PostgreSQL + Worker”，否则面试追问很容易露出没有真正实现。

## 11. 当前项目如何讲

项目主线应是：

1. 先做最小 Function Calling 闭环。
2. 发现工具参数、异常、超时和上下文问题，抽象 ToolRegistry/ToolExecutor。
3. 工具变多后加入 deferred discovery、MCP 和 Hook。
4. 长对话引入 Session、历史搜索、结构化长期记忆和异步 consolidation。
5. 多入口引入 MessageBus、同 session 串行和跨 session 并发。
6. 通过真实 Bug 补齐 correlation、reasoning 回放、SSRF、rollback 和 graceful shutdown。
7. 下一版用 LangSmith/eval 把经验固化为可回归的工程指标。

这条演进链比“参考了哪个项目”更能说明你真正理解并解决了 Agent 工程问题。
