# Kirakira Agent 版本演进与后续路线图

## 1. 文档说明

当前仓库没有按 `v0.1.0`、`v0.2.0` 逐版打 Git tag，因此本文采用两套标识：

- **真实阶段**：以实际 commit 和 PR 为准，可回到代码核验。
- **建议版本号**：用于理解产品成熟度和规划下一版，不代表仓库已经发布对应 tag。

整体演进可以概括为：

```text
最小 Tool Harness
      ↓
被动 Agent Runtime 骨架
      ↓
工具与生命周期补齐
      ↓
可靠性和安全边缘加固
      ↓
完整被动链路审计版（当前）
      ↓
可观测、可恢复、可部署的生产版
```

## 2. 阶段总览

| 建议版本 | 真实节点 | 核心目标 | 阶段结果 |
| --- | --- | --- | --- |
| V0.1 Harness | `2fcd52d` 及更早提交 | 跑通模型与工具闭环 | 可学习、可演示 |
| V0.2 Passive Runtime | `cd7230b` / PR #1 | 建立完整被动架构骨架 | 从单进程 Demo 升级为多渠道 Runtime |
| V0.3 Tool Expansion | `0ba866c` / PR #2 | 补齐常用工具和生命周期事件 | Agent 能完成更真实的任务 |
| V0.4 Reliability | `688d316` / PR #3 | 修复安全、队列和 Channel 边缘问题 | 从“能跑”转向“稳定运行” |
| V0.5 Complete Passive | `6c288b4` / PR #5 | 对照 Reference 深度审计并系统补齐 | 当前完整被动链路版本 |
| V0.6 Observability | 下一版建议 | 可观测、可恢复、可评测 | 面向长期运行和团队开发 |
| V0.7 Memory Intelligence | 后续建议 | 提高记忆质量和检索判断 | 从“有记忆”升级为“会管理记忆” |
| V0.8 Ecosystem | 后续建议 | Dashboard、A2A、插件生态 | 面向用户和扩展开发者 |
| V1.0 Production | 长期目标 | 部署、迁移、安全和 SLA | 可公开交付的稳定版本 |

## 3. V0.1：最小 Agent Harness

### 3.1 当时解决的问题

项目最初关注的是最小 Agent 闭环：

```text
用户输入 -> LLM -> Tool Call -> Tool Result -> LLM 最终回复
```

主要模块是 `agent.py`、OpenAI-compatible model client、ToolRegistry、基础文件/Shell 工具和 CLI。模型负责决定是否调用工具，Harness 负责执行和回填结果。

### 3.2 完成内容

- OpenAI-compatible chat completion。
- 基础 tool calling 循环。
- Shell、读写文件、编辑文件。
- Skills 加载和上下文压缩雏形。
- CLI REPL。
- 少量单元测试和中英文 README。

### 3.3 价值

这个阶段非常适合解释 Agent 的本质：Agent 并不等于一个超长 Prompt，而是“模型决策 + 工具执行 + 状态回填”的循环系统。代码量少，便于理解模型协议和工具消息格式。

### 3.4 局限

- 只有同步调用，没有真正的消息入口抽象。
- 没有 session 隔离，无法服务多个用户。
- 没有长期记忆和历史检索。
- 没有插件、MCP、生命周期或 Hook。
- 工具超时、取消、并发和错误恢复都很弱。
- 更像教学 Harness，不适合长期运行。

### 3.5 为什么必须升级

如果继续把功能堆进 `Agent.run()`，Channel、记忆、插件和持久化会互相耦合。V0.2 的首要任务不是继续加工具，而是建立清晰的运行时边界。

## 4. V0.2：被动 Runtime 骨架

真实节点：`cd7230b Add passive agent runtime channels`，后经 PR #1 合入。

### 4.1 升级目标

参考 akashic-agent，将“用户发消息后 Agent 回复”拆成稳定的数据流：

```text
Channel -> MessageBus -> AgentLoop -> PassiveTurnPipeline
        -> Context/Memory/Tool Loop -> OutboundMessage -> Channel
```

### 4.2 完成内容

- 新增统一 `InboundMessage` / `OutboundMessage`。
- 新增异步 MessageBus 和 ChatLane。
- 新增 AgentLoop、PassiveTurnPipeline、DefaultReasoner。
- 新增 EventBus 和 lifecycle context。
- 新增 Session/SessionManager，保存完整 tool chain。
- 新增 Markdown Memory、RECENT_CONTEXT 和 SELF。
- 新增 ContextBuilder，注入时间、记忆、skills 和 workspace。
- 新增 Tool Hook 和本地插件管理器。
- 接入 Web、Telegram、QQ/OneBot、CLI。
- 保留旧同步 Agent API，避免一次性破坏兼容性。
- 增加 Runtime 和 Channel 集成测试。

### 4.3 关键架构变化

这一版最重要的变化不是功能数量，而是职责拆分：

- Channel 只处理平台协议。
- Bus 只处理消息路由和顺序。
- Loop 只驱动 turn。
- Pipeline 负责一个 turn 的阶段编排。
- Reasoner 负责 LLM Tool Loop。
- Session 和 Memory 分别管理短期历史与长期事实。

### 4.4 升级收益

- Web、Telegram、QQ 共享相同 Agent 行为。
- 不同用户可以拥有独立 session。
- 插件和记忆不再写死在 CLI。
- 后续功能有明确挂载点，不需要继续扩大 `agent.py`。

### 4.5 当时仍有的问题

- 工具种类仍少，外部研究和历史回源能力有限。
- streaming、MCP、复杂插件 descriptor 尚未完整实现。
- Channel 只覆盖基础消息，附件、流式编辑和管理 API 较弱。
- 并发和 graceful shutdown 的边缘情况不充分。
- 记忆仍以简单词法和显式规则为主。

## 5. V0.3：工具与生命周期补齐

真实节点：`0ba866c Fill passive runtime tool gaps`，后经 PR #2 合入。

### 5.1 升级目标

在架构骨架稳定后，补齐 Agent 完成真实任务所需的“手和眼睛”，同时增加关键运行事件。

### 5.2 完成内容

- 增加 `list_dir`、`web_fetch`、`web_search`、`tool_search`。
- 增加 `message_push`。
- 增加 `memorize`、`recall_memory`、`forget_memory`。
- 增加 `search_messages`、`fetch_messages`。
- 增加 ToolCallStarted、ToolCallCompleted 等 lifecycle event。
- 加强工具 Hook deny 和错误记录测试。
- 编写第一版 Reference 差异审计。

### 5.3 关键架构变化

工具不再只是静态“函数列表”，而开始承担以下能力：

- 通过 task-local context 获取当前 session/channel/chat。
- 通过 Hook 在执行前改参或阻断。
- 通过 lifecycle event 暴露运行状态。
- 通过 `tool_search` 为未来 deferred tool discovery 铺路。

### 5.4 升级收益

- Agent 可以访问网页、搜索历史和管理长期记忆。
- 插件可以观察工具开始/结束，而不必侵入 Reasoner。
- `message_push` 让工具结果可以经过统一 Bus 发往 Channel。
- 差异审计开始从“凭印象复刻”转向“按组件核对”。

### 5.5 当时仍有的问题

- `web_fetch` 仍可能面临 SSRF 和重定向绕过。
- Shell、文件写入和错误返回的安全边界不够严格。
- 同一 chat 的 outbound、Web pending request、Channel shutdown 仍有竞态。
- 记忆重复写入和 session 删除后的记忆污染尚未完全解决。
- 流式模型协议和 deferred tool 仍不完整。

## 6. V0.4：可靠性与安全加固

真实节点：`688d316 Harden passive runtime audit gaps`，后经 PR #3 合入。

### 6.1 升级目标

对“正常演示能跑，但异常情况下容易坏”的路径进行加固。

### 6.2 完成内容

- 修复 outbound queue completion 和 drain 行为。
- 清理 Web pending future，避免超时后泄漏。
- 加强 QQ 消息去重和 OneBot `status/retcode` 检查。
- Telegram 长消息按平台限制分片。
- CLI 避免重复启动多个 Runtime loop。
- 同步 Registry 正确处理 async tool。
- `web_fetch` 默认阻止 localhost、私网和特殊地址。
- 文件读取增加 UTF-8 容错。
- Shell timeout 上限和危险命令检查加强。
- `memorize` 与 post-turn consolidation 去重。
- 增加相应回归测试。

### 6.3 关键架构变化

这一版没有大规模新增子系统，而是把失败路径当成一等公民：

- 队列消息必须有明确完成状态。
- 外部 API 的 HTTP 200 不等于业务成功。
- 超时 future 必须删除。
- 工具返回错误时必须进入统一错误语义。
- 网络工具必须假设模型可能被 Prompt Injection 诱导访问内网。

### 6.4 升级收益

系统从“功能清单较完整”变成“在超时、重复消息、长回复和失败 API 下仍能收口”。这一步对实际部署的价值高于继续增加普通工具。

### 6.5 当时仍有的问题

- 仍缺完整 SSE streaming 和 DeepSeek reasoning 回放。
- MCP、插件 manifest/config/KV 尚未深入覆盖。
- Session 还是较简单的 JSON 检索。
- Memory 没有 embedding、后台结构化 consolidation 和撤销链。
- Subagent、显式调度和后台 Shell 管理尚不完整。

## 7. V0.5：完整被动链路审计版（当前）

真实节点：`6c288b4 Complete passive agent runtime audit`，PR #5。

### 7.1 本版目标

再次逐目录核对 Reference，排除 proactive/drift 后，系统性补齐被动 Runtime 的核心行为、资源生命周期、扩展系统、测试和文档。

### 7.2 核心 Runtime

- 同 session 串行、跨 session 并行。
- 同 chat outbound ticket 保序、跨 chat 并发发送。
- `/stop` 取消 turn，并保存 partial reply/thinking/tool chain。
- slash command 在昂贵记忆检索前执行。
- BeforeTurn、BeforeReasoning、PromptRender、BeforeStep、AfterStep、AfterReasoning、AfterTurn 扩展点。
- EventBus ordered handler、fanout observer 和 shutdown 等待。
- CoreRuntime 统一管理所有后台服务的启停顺序。

### 7.3 模型和 Tool Loop

- OpenAI-compatible SSE streaming。
- fragmented tool call 参数重组。
- DeepSeek `reasoning_content` 完整保存与回放。
- 模型 timeout、429/5xx retry、空回复 retry。
- context length/content safety 分级裁剪。
- 重复工具调用保护和最大迭代阶段总结。
- deferred tool、`tool_search select:name` 和每 session 5 项 LRU。
- disabled tool 不暴露且不能绕过直接调用。

### 7.4 Session 和 Memory

- JSON canonical session 原子写入。
- session key 可读前缀 + hash，避免清洗碰撞。
- reasoning/tool history 无损重建。
- SQLite FTS5 trigram 消息索引和 JSON 回源。
- session list/search/fetch/delete。
- Markdown 托管记忆区和人工内容共存。
- typed memory record、source_ref、reinforcement、forget。
- 中文 bigram/英文 token 词法检索。
- 可选 embedding 语义+词法混合召回。
- memory type/time filter。
- session 删除时撤销源自该 session 的记忆。
- 回复后异步 LLM consolidation；下一轮等待前一轮收口。

### 7.5 插件和 MCP

- `.aka-plugin/plugin.json` descriptor。
- config、config.local、可选 ConfigModel、原子 KV、独立 data dir。
- `@tool`、`@on_tool_pre` 和 phase decorators。
- 插件 tools、skills、channels 和 MCP 声明。
- 加载失败回滚、坏插件隔离、逆序幂等 terminate。
- plugin install/list/doctor，安装后重启生效。
- stdio MCP JSON-RPC client，支持 initialize、并发 request、tools/list、tools/call、timeout、stderr 和 disconnect。
- MCP server 动态 add/remove/list 与配置持久化。

### 7.6 Channel 和后台能力

- Web 并发 request correlation、unsolicited event long poll、session/memory 管理 API。
- Telegram 附件、长消息、429 retry、streaming live edit、出站文件。
- QQ token 鉴权、私聊/群聊策略、发送者身份、图片/文件和 retcode 校验。
- inline/background subagent、独立 session、权限 profile、并发上限、list/cancel 和 completion 回注。
- 用户显式 schedule/list/cancel 持久化调度。
- 后台 Shell、`task_output`、`task_stop` 和进程组回收。

### 7.7 验证结果

- 使用本地 conda Python 3.12 环境。
- `compileall` 通过。
- 83 项 unittest 全部通过。
- `git diff --check` 通过。
- `deepseek-v4-flash` 普通在线请求通过。
- 在线 SSE Tool Loop 收到 41 个 delta。
- 模型真实调用 `write_file`、`read_file`，session 保存两组 tool chain。
- 在线 consolidation 推进 `last_consolidated=6`，生成两条可检索记忆并写入 HISTORY。

### 7.8 当前版本的定位

V0.5 已经不是教学 Demo，而是一套完整、可扩展、可在线运行的被动 Agent Runtime。它适合：

- 个人多渠道 AI 助手。
- Agent 架构学习和二次开发。
- MCP/插件工具宿主。
- 简历和工程能力展示。
- 后续生产化演进的代码基础。

它仍不应直接宣称为“生产级 V1.0”，因为可观测性、数据迁移、负载测试、安全策略和运维能力还需要单独建设。

## 8. 当前版本的升级空间

### 8.1 P0：生产可靠性

1. **结构化日志与 Trace ID**
   当前已有 lifecycle 和 correlation id，但缺少统一 JSON log、跨 turn trace、模型耗时和工具耗时查询。

2. **指标与健康检查**
   增加 `/healthz`、`/readyz` 和 Prometheus 指标：queue depth、active turns、LLM latency、tool error rate、memory worker backlog、MCP status。

3. **崩溃恢复**
   当前内存中的 running subagent、shell task 和 pending HTTP request 在进程重启后不能恢复。应引入任务 journal 和 startup reconciliation。

4. **数据迁移**
   Session JSON、memory items 和 schedule schema 需要显式 `schema_version`、migration runner 和备份/回滚。

5. **容量与压力测试**
   增加 10/50/100 并发 session、慢模型、慢 Channel、MCP 断连、超大历史和持续 streaming 测试。

### 8.2 P1：安全与权限

1. 为工具增加 `read-only/write/network/process/admin` capability。
2. 将当前 profile disabled list 升级为 deny-by-default capability policy。
3. 增加 Shell 命令审计、可恢复删除和 workspace quota。
4. 对插件安装增加 commit pin、hash、签名或 allow-list。
5. 对 Web 管理 API 增加认证、CSRF/CORS 和速率限制。
6. 对附件增加 MIME 深检、文件名清洗、总容量和生命周期回收。

### 8.3 P1：记忆智能

1. 使用轻量模型做“是否需要历史检索”的 route gate。
2. 增加 query rewrite 和多 query retrieval。
3. 引入语义 dedup，而不仅是 exact dedup。
4. 区分 identity、preference、procedure、event 的不同保留和排序策略。
5. 增加 evidence confidence、矛盾检测和更新替代关系。
6. 让 PENDING 真正承担缓存稳定和批量归档，而非只作为扩展点。

### 8.4 P2：生态和体验

1. 外部 A2A Peer Agent 的发现、冷启动、提交、轮询和回注。
2. 独立 Dashboard：session、memory、plugin、MCP、task、trace 和指标。
3. WebSocket transport，降低 Web streaming 延迟。
4. 插件 SDK、模板、兼容测试和版本约束。
5. 更多 Channel adapter，例如 Discord、Slack、企业微信。

## 9. 下一版建议：V0.6 Observability & Recovery

下一版不建议继续大规模堆功能。当前最值得做的是让系统“出了问题能看见，重启以后能恢复，升级以后不丢数据”。

### 9.1 版本目标

```text
可观测：每个 turn 都能追踪
可诊断：错误能定位到模型、工具、插件、MCP 或 Channel
可恢复：后台任务和持久化状态在重启后有明确结论
可迁移：数据格式升级不依赖人工修改文件
可评测：性能和正确性有固定基线
```

### 9.2 建议开发顺序

#### Milestone 1：统一 Trace

- 新增 `TraceContext(trace_id, turn_id, session_key, parent_id)`。
- Inbound、lifecycle、LLM request、tool call、outbound 全程传播。
- JSONL 或 SQLite trace store。
- 敏感字段脱敏，禁止记录 API key 和完整私密附件。
- Web API 可按 trace id 查询事件时间线。

验收：一条包含两次工具调用的 turn，可以看到每阶段开始/结束、输入摘要、耗时、状态和错误。

#### Milestone 2：Metrics 与健康检查

- `/healthz` 只检查进程存活。
- `/readyz` 检查 session DB、memory store、MessageBus、必要 MCP 和 Channel。
- counters：turn/tool/model/channel 成功与错误。
- histograms：LLM/tool/turn latency。
- gauges：queue depth、active session、background task、memory backlog。

验收：故意断开 MCP 或让模型超时，readiness 和指标能准确反映故障。

#### Milestone 3：持久化任务 Journal

- 为 subagent、schedule、background shell 定义统一 JobRecord。
- 状态机：pending/running/completed/failed/cancelled/interrupted。
- 启动时扫描 running 记录并标记 interrupted 或按策略恢复。
- completion 回注幂等，避免重启后重复回复。

验收：后台任务运行时强制结束进程，重启后不会永远显示 running，也不会重复回注。

#### Milestone 4：Schema Migration

- Session、memory、schedule、plugin registry 加 `schema_version`。
- migration 按版本顺序执行，写前自动备份。
- migration 失败保持旧文件可恢复。
- 增加旧版本 fixture 测试。

验收：从 V0.5 fixture 启动 V0.6，所有 session 和记忆可读，失败迁移不破坏源文件。

#### Milestone 5：压力与故障测试

- 50 个并发 session，同 session 10 条连续消息。
- streaming 中断、Channel 慢发送、MCP 子进程退出。
- 10 MB 附件、长 tool output、超长历史。
- 记录 p50/p95/p99 和资源占用。

验收：无死锁、无 orphan process、无 session 串线、关机在预算时间内完成。

### 9.3 V0.6 不建议做的事情

- 不同时重写全部 Memory 算法。
- 不在 Trace 基础缺失时先做复杂 Dashboard。
- 不把主动链路重新混入 PassiveTurnPipeline。
- 不为了“代码更像 Reference”复制当前没有真实需求的抽象。

## 10. V0.7：Memory Intelligence 建议

V0.6 建立观测与评测后，再升级记忆，才能判断算法是否真的改善效果。

建议内容：

- fast model memory route gate。
- query rewrite、HyDE 和多查询召回。
- lexical/vector/source evidence 的 RRF 融合。
- semantic dedup 和矛盾检测。
- procedure rule schema 与工具/skill 关联。
- profile extraction 和 SELF 更新审核。
- PENDING 批处理与 prompt cache 稳定策略。
- LongMemEval/PersonaMem 风格离线评测。

核心验收不应是“模块都写了”，而应是：

- 需要历史的问题召回率提高。
- 无关问题不注入多余记忆。
- 错误记忆可追溯、可撤销。
- token 和 latency 增长在预算内。

## 11. V0.8：生态与用户体验建议

- A2A Peer Agent adapter。
- Dashboard 和 WebSocket。
- 插件 SDK、脚手架和兼容矩阵。
- MCP server 状态、重连和权限 UI。
- session 导入导出、记忆审核和任务管理。
- 安装向导与生产配置检查器。

这一版的目标是降低使用和扩展门槛，而不是继续改变 AgentLoop 核心语义。

## 12. V1.0 的建议准入标准

达到以下条件再考虑发布 V1.0：

- 核心数据有 schema version、迁移和备份。
- 关键链路有结构化 trace 和指标。
- 通过固定并发、故障注入和长时间运行测试。
- Web 管理接口具备认证和速率限制。
- 工具与插件采用 capability 权限模型。
- 后台任务重启后状态明确且 completion 幂等。
- 至少一个真实 Telegram 或 QQ 长时间运行验证。
- 文档覆盖安装、配置、升级、备份、恢复和故障排查。
- 保持主动链路与被动 Runtime 的依赖方向清晰。

## 13. 如何使用这份路线图

### 用于项目开发

每一版先确定“一个主要工程问题”，再增加功能。建议顺序是可靠性、可观测、记忆质量、生态体验。

### 用于简历讲述

可以把项目演进描述为：

1. 从最小 Tool Calling Harness 起步，理解 Agent 基本循环。
2. 通过 MessageBus、Session 和 Pipeline 完成多渠道 Runtime 重构。
3. 增加 Memory、Plugin、MCP、Subagent 和 Streaming。
4. 通过并发、取消、SSRF、进程回收和在线测试完成可靠性加固。
5. 下一阶段面向 observability、recovery 和 schema migration 生产化。

这比简单罗列“接了多少工具”更能体现架构设计、工程治理和持续演进能力。

### 用于版本决策

- 当前要稳定运行：优先 V0.6。
- 当前要提高个性化：完成 V0.6 基础观测后进入 V0.7。
- 当前要展示产品：可并行做轻量 Dashboard，但不要跳过认证和 trace。
- 当前要复刻 Reference 更多能力：优先 A2A 和记忆检索策略，不复制 proactive/drift。

## 14. 结论

Kirakira Agent 的前几轮升级是合理的：先搭 Runtime 骨架，再补工具，随后处理边缘安全，最后完成全链路审计。当前最明显的风险已经不再是“缺少某个普通工具”，而是长期运行后的可观测性、恢复能力、数据迁移和安全治理。

因此，下一版最优路线不是继续扩大功能面，而是建设 V0.6 Observability & Recovery。完成这一层后，再升级高级记忆和扩展生态，项目会更稳，也更容易向 V1.0 演进。
