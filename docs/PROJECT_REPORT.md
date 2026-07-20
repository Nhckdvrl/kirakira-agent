# Kirakira Agent 项目报告

## 1. 项目概述

Kirakira Agent 是一个面向真实运行场景的多渠道 AI Agent Runtime。项目以 `akashic-agent` 的被动回复架构为参考，将原有的最小 tool-calling demo 重构为包含消息入口、会话并发、长期记忆、流式工具循环、插件、MCP、子 Agent、调度和完整生命周期管理的系统。

项目刻意不包含自主主动链路：Agent 不会在没有用户请求的情况下自行读取数据源、判断是否触达用户，也不执行 proactive/drift 自治循环。Web、Telegram、QQ、用户显式定时任务和当前对话派生的后台工作均属于被动请求的延伸，完整保留。

## 2. 系统全景

```text
Web / Telegram / QQ / CLI
              |
              v
       InboundMessage
              |
              v
         MessageBus
              |
              v
 AgentLoop: cross-session parallel
            same-session serial
              |
              v
     PassiveTurnPipeline
              |
     +--------+---------+
     | session/history  |
     | memory retrieval |
     | skills/plugins   |
     +--------+---------+
              |
              v
 Context store + retrieval request
              |
              v
 PromptBlock -> PromptAssembler
 stable system + dynamic Context Frame
              |
              v
 DefaultReasoner streaming loop
              |
      ToolExecutor + hooks
              |
  builtins / MCP / plugin / spawn
              |
              v
 Session commit + lifecycle events
              |
              v
       OutboundMessage
              |
              v
       original Channel

After reply: asynchronous LLM memory consolidation
Before next same-session turn: wait for previous consolidation
```

## 3. 代码结构

```text
kirakira_agent/
  cli.py                    Runtime 装配、配置读取、CLI/serve 入口
  runtime.py                DefaultReasoner、Pipeline、AgentLoop、CoreRuntime
  events.py                 Channel/Bus 消息合同
  bus.py                    队列、ChatLane、dispatch 和 graceful drain
  lifecycle.py              7 phase ctx 与 turn/tool/stream/context 事件
  event_bus.py              ordered interception 和 fanout observer
  session.py                JSON session、FTS 索引、history reconstruction
  memory.py                 Markdown/typed memory 与后台 consolidation
  embeddings.py             OpenAI-compatible embedding client
  context_builder.py        PromptBlock 与消息装配入口
  prompting/
    blocks.py               具名 block、优先级、静态 section cache
    assembler.py            stable system + dynamic Context Frame
    budget.py               具名语义裁剪计划
  tool_hooks.py             pre/post/error 工具 hook executor
  plugins.py                插件加载、回滚、配置、KV 和管理工具
  plugin_manifest.py        插件发现与启停清单（manifest.toml）
  plugin_decorators.py      tool/hook/phase decorators
  snapshot.py               运行时能力代际快照、租约与组合工具视图
  context_policy.py         派生窗口、输入预算与 provider-facing token 估算
  scheduler.py              用户显式定时消息
  subagent.py               inline/background 子 Agent
  channels/
    contract.py             Channel 和 ChannelContext 协议
    host.py                 多 Channel 生命周期
    web.py                  HTTP chat、events、session/memory API
    telegram.py             Telegram Bot API
    qq.py                   QQ OneBot HTTP
  mcp/
    client.py               stdio JSON-RPC MCP client
    declarations.py         servers/*.toml 严格解析与内容 revision
    host.py                 按代际连接整批候选 server
    publisher.py            把 catalog 编译进快照并原子换代
    watcher.py              轮询 revision，串行发布变化
  models/
    openai_compatible.py    chat completion、SSE、retry、DeepSeek 兼容
  tools/
    registry.py             schema、ContextVar、同步/异步执行
    builtins.py             文件、shell、网络、视觉、记忆、消息工具
```

旧 `agent.py` 同步最小 API 仍然保留，用于兼容和教学；完整产品路径从 `cli.build_runtime()` 进入。

## 4. 一条消息如何流动

### 4.1 Channel 入站

每个 Channel 只负责平台协议转换：

1. 验证 token、allow list、群策略和消息格式。
2. 下载并校验附件，保存到工作区 `uploads/`。
3. 生成 `InboundMessage(channel, sender, chat_id, content, media, metadata)`。
4. 调用 `MessageBus.publish_inbound()`。

Channel 不构建 prompt、不调用模型、不直接写 session，因此 Web、Telegram、QQ 可以共享完全相同的 Agent 行为。

### 4.2 MessageBus 与并发

`MessageBus` 有独立 inbound/outbound queue。`AgentLoop` 从 inbound 消费后，为每个消息创建 turn task：

- session key 默认是 `channel:chat_id`，也可由内部 completion metadata 覆盖。
- 每个 session 对应一个 `asyncio.Lock`，保证历史和记忆的写入顺序。
- 不同 session 不共享 lock，慢工具或慢模型不会阻塞其他用户。
- outbound 使用 per-chat ticket，保证同一聊天按生成顺序发送。

当用户执行 `/stop` 时，Channel 根据 session key 调用 interrupt callback。AgentLoop 取消当前 task，并把已执行的工具链和 partial stream 快照写入 session，后续对话仍可恢复上下文。

### 4.3 BeforeTurn

`PassiveTurnPipeline.run()` 先发出 `TurnStarted`。斜杠命令在昂贵的记忆检索前执行，使 `/tools`、`/skills`、`/memory` 和插件命令能够快速 abort。

普通消息执行：

1. 等待同 session 上一轮 memory consolidation，并检查未归档区安全阈值。
2. 从 `last_consolidated` 开始重建模型历史，tool result 过长时保留头尾与总行数。
3. 用当前消息、完整候选历史、session/channel metadata 构造 `RetrievalRequest` 并执行长期记忆召回。
4. 识别 `$skill-name`。
5. 依次执行 EventBus handler 和插件 BeforeTurn modules。

插件可以修改历史、追加记忆候选、激活 skill、追加 hint，或直接返回回复终止本轮。

### 4.4 BeforeReasoning 与 PromptRender

Pipeline 使用 `ContextVar` 绑定当前 `session_key/channel/chat_id/timestamp`。这个上下文只在当前 async task 中可见，避免并发 turn 的工具路由串线。

Prompt 不再由一个函数拼成长字符串，而是分为具名 block：

- stable system：`identity`、`behavior_rules`、`skills_catalog`、`self_model`、
  `long_term_memory`、`session_context`；静态 block 按 workspace + 内容签名缓存。
- dynamic Context Frame：`recent_context`、`active_skills`、`retrieved_memory`、
  `turn_injection`、`plugin_hints`；它位于历史之后、当前用户消息之前，并明确标记为系统候选上下文，
  不是用户陈述。
- current user：带时区的今天/昨天/明天信封、用户原文和附件引用。

Skill 正文由 `$skill-name` 显式激活；frontmatter 写 `always: true` 的 skill 每轮自动进入
`active_skills`。`PromptRenderCtx` 允许插件增加具名 top/bottom section、禁用 section、追加
turn injection/hint 或调整历史；每个 retry plan 都会重新执行 prompt hooks 并重新 render。

输入预算统一计算 system、messages、工具 schema 和图片，公式为
`floor(context_window × effective_context_percent) - max_tokens`。超限时按
`skills_catalog → recent_context → long_term_memory → retrieved_memory → history 50% → history 0`
重新渲染；历史切片始终回退到 user boundary。Runtime 发出 `ContextPrepared`，Provider 在真正发
HTTP 请求前再预检一次，以覆盖 ReAct 中途解锁的新工具 schema。

## 5. LLM Tool Loop

`DefaultReasoner.run_turn()` 是被动回复的推理核心。

每轮步骤：

1. 根据 always-on、deferred LRU 和 disabled set 计算工具 schema，并发出带输入估算的 BeforeStep。
2. 调用 OpenAI-compatible client；启用 stream 时解析 SSE。
3. 将正文和 reasoning 增量发布为 `StreamDeltaReady`。
4. 没有 tool call 时返回最终回复。
5. 有 tool call 时写入 assistant tool-call message。
6. 对每个 call 发出 ToolCallStarted。
7. ToolExecutor 执行 pre hooks、真实工具、post hooks。
8. 写入 tool result，发出 ToolCallCompleted、AfterStep。
9. 下一 iteration 将完整 tool history 再交给模型。

保护策略：

- 参数 schema 失败不会调用 handler。
- disabled/deferred 工具无法绕过可见性直接调用。
- 相同工具及参数重复达到阈值后阻断，防止死循环。
- model timeout 会取消本轮；shell cancellation 同时清理进程组。
- context length/content safety 按裁剪计划重试。
- 空回复重试一次。
- 达 iteration 上限时再请求一次无工具阶段总结。

DeepSeek thinking 模式下，每个包含工具调用的 assistant message 都保存并回放 `reasoning_content`，符合其多轮工具协议。

每次模型响应还会采集 provider `prompt_tokens/completion_tokens/total_tokens`。最终
`context_trace` 保存所有 attempt、section chars/token estimate、cache hit、选中计划、ReAct
request 数和实际 usage；commit 后再计算下一轮 history baseline，写入 session metadata 并发出
`ContextBudgetUpdated`。

## 6. 工具体系

### 6.1 ToolRegistry

ToolRegistry 保存 `ToolSpec + handler + deferred`，提供：

- JSON schema 暴露和执行前校验。
- `execute()`/`execute_async()` 双入口。
- 同步 handler 自动放入 worker thread，不阻塞 event loop。
- task-local context。
- 动态 register/unregister。
- runtime shutdown callback。

### 6.2 ToolExecutor 与 Hook

执行顺序固定为：

```text
pre_tool_use -> invoke -> post_tool_use
                       -> post_tool_error
```

pre hook 可以改参数或 deny。pre hook 自身异常采用 fail-closed，防止安全插件失效后仍执行危险动作；post hook 是观察型，单个失败不会撤销已经完成的工具副作用。

### 6.3 内置工具

- 文件：`list_dir`、`read_file`、`write_file`、`edit_file`。
- Shell：`bash`、`task_output`、`task_stop`。
- 网络：`web_fetch`、`web_search`。
- 上下文：`load_skill`、`compact`、`tool_search`、`vision`。
- 记忆：`memorize`、`recall_memory`、`forget_memory`。
- 历史：`search_messages`、`fetch_messages`。
- 消息：`message_push`。
- 扩展：MCP、plugin management、`spawn`、`spawn_manage`。
- 调度：`schedule`、`list_schedules`、`cancel_schedule`。

后台 shell 的 stdout/stderr 合并写入 `.kirakira/shell-tasks/*.log`。模型可轮询 offset；停止、timeout、turn cancellation 和 runtime shutdown 都会杀死整个进程组并删除日志。

`web_fetch` 每次请求和 redirect 都做 DNS/IP 校验，默认阻止 localhost 与私网；这是对 prompt injection 后 SSRF 的基础隔离。

## 7. Session 与消息检索

Session JSON 是事实源，保存：

- user/assistant 内容、时间和 media。
- reasoning、tools_used 和完整 tool_chain。
- interrupted/partial_reply。
- channel/chat/username/last_sender/turn count/tool count。
- `last_consolidated`。
- assistant 消息级 `context_trace` 与 session 级 `context_budget`。

文件名采用可读 key 加 hash，写入为原子 replace。`get_history()` 从 user boundary 开始，将持久化 tool chain 重新展开为 OpenAI-compatible assistant/tool message 序列。

SQLite `message_index.sqlite3` 是可重建的 FTS5 trigram 索引。`search_messages` 返回稳定 `source_ref=session:key:index`，`fetch_messages` 再从 JSON 事实源读取前后文。

## 8. 长期记忆

运行文件：

```text
memory/MEMORY.md          人工区 + Runtime 托管长期记忆 block
memory/SELF.md            自我模型
memory/RECENT_CONTEXT.md  近期 turn 摘要
memory/HISTORY.md         带 source marker 的时间线
memory/PENDING.md         后续 optimizer 的稳定扩展点
memory/items.json         类型化记忆事实源
```

记忆记录包括 id、content、type、source_ref、status、reinforcement、created/updated time 和可选 embedding。

召回流程：

1. 中文 bigram、英文 token、substring 形成 lexical lane；不匹配的记录不入选。
2. 若配置 embedding，则请求 `/embeddings` 并以 cosine 阈值形成 vector lane；查询失败可降级到 lexical。
3. 使用 RRF（`k=60`，lexical lane 权重 0.5）按各 lane 的名次融合，而不是相加尺度不可比的原始分。
4. 用 reinforcement + 14 天半衰的 hotness 乘数调整融合结果。
5. 应用 type/since/until 过滤，并在 1200 chars / 单行 180 chars 注入预算内生成 retrieved block。

回复后先同步更新 RECENT_CONTEXT/HISTORY，再调度后台 LLM consolidation。达到窗口后，模型只抽取用户明确表达的稳定身份、偏好、流程和事件；assistant 建议不会作为用户事实。结果使用 JSON parser、source_ref 和 dedup 写入。

## 9. 插件与 MCP

插件扫描 `<workspace>/.kirakira/plugins` 和 `<workspace>/plugins`，以根目录 `plugin.py` 为唯一标志。能力由代码声明（`skill_roots()`、`mcp_servers()`、phase 方法、decorators）；`.kirakira/manifest.toml` 只记录 `plugin_id` + `enabled`，清单损坏时 fail loud。

`PluginContext` 提供 workspace、session、memory、event bus、tool registry、独立 data dir 和原子 KV。

MCP client 使用 Content-Length framed stdio JSON-RPC：

- `initialize` handshake。
- request id -> Future 并发关联。
- `tools/list` 和 `tools/call`。
- timeout、server error、stderr drain、异常退出。
- 拒绝非对象 result 与非数组 content。

workspace MCP 由 `<workspace>/mcp/servers/*.toml` 声明，watcher 按内容 revision 热重载；插件 MCP 走同一个 publisher，只是 source 不同。两者共用整批候选语义：任一声明非法或任一 server 连不上，整批作废，旧代际继续服务。

MCP 工具挂在运行时快照上而非共享 ToolRegistry，因此换代不会影响在途 turn；旧代际的进程等最后一个租约释放后才断开。远端工具默认 deferred，模型用 `tool_search select:name` 解锁后才可调用。契约见 [_handbook/workspace-mcp.md](../_handbook/workspace-mcp.md) 与 [_handbook/snapshot-and-lease.md](../_handbook/snapshot-and-lease.md)。

## 10. Subagent 与显式调度

`spawn` 支持 inline 和 background：

- 子 Agent 使用独立 session 和最大 20 iteration。
- research 禁止 shell/write/edit；scripting 禁止网络/vision；general 同时具备两类能力。
- 所有 profile 都禁止递归 spawn、消息推送、MCP/插件修改和定时任务。
- background 最大 3 个，结果写入 `.kirakira/subagent-runs/<id>/result.json`。
- `spawn_manage` 提供 list/cancel。
- completion 以 `omit_user_turn` 内部消息回注原 session，再由主 Agent 组织用户回复。

Scheduler 只处理用户明确创建的 fire time/interval，不执行自主判断。任务写入 `.kirakira/schedules.json`，到期后向原 channel 发布 OutboundMessage。

## 11. Channel 细节

### CLI / TUI

交互终端默认启动项目自有的 Textual TUI；tmux 只用于保活，不参与界面实现。每次无 `--session`
启动都会生成新的空白本地 Session，`/sessions` 用键盘选择并恢复历史，`/session <name>` 直接切换或
创建命名 Session。TUI 和 `--plain` 共用 `TurnViewState`：stream delta 是 draft，
`TurnFinished.outbound` 是唯一权威终态并替换 draft，避免把最终全文重复追加到流式前缀。

完整界面与 Session 合同见 [_handbook/cli-and-sessions.md](../_handbook/cli-and-sessions.md)。

### Web

标准库 ThreadingHTTPServer 提供 chat 页面、消息 API、`/events` 长轮询、interrupt，以及 session/memory 管理端点。每次请求携带 client request id，解决同 session 并发 HTTP 请求拿错回复的问题。

### Telegram

直接对接 Bot API long polling。支持 allow list、图片/文档、reply context、长文本分片、429 retry、出站文件。SSE 增量先发送占位消息，再持续 edit，完成后定稿。

### QQ

接收 NapCat/OneBot HTTP webhook，支持 bearer/query token、私聊与逐群策略、require_at、CQ/structured media、发送者身份和去重。出站调用 OneBot API，并验证 HTTP、`status` 和 `retcode`。

## 12. 配置与运行

核心配置使用 `config.toml`，环境变量优先：

```toml
[llm.main]
model = "deepseek-v4-flash"
api_key = "${DEEPSEEK_API_KEY}"
base_url = "https://api.deepseek.com/v1"
enable_thinking = false
context_window = 1000000

[agent]
max_iterations = 40
max_tokens = 8192

[agent.context]
effective_context_percent = 0.9

[channels.chat]
enabled = true
host = "127.0.0.1"
port = 8765
```

运行：

```bash
python -m kirakira_agent
python -m kirakira_agent --serve
python -m kirakira_agent --serve --web --telegram --qq
```

## 13. 验证记录

本轮实际使用已有 conda 环境：

```text
/home/xiang/.conda/envs/xingshu-vllm/bin/python
Python 3.12
```

自动化结果：

- `compileall` 通过。
- unittest 共运行 186 项：183 项通过，3 项按可选环境条件跳过。
- `git diff --check` 通过。
- fake MCP stdio server 完成 initialize/list/call/error/disconnect 集成测试。
- Web/Telegram/QQ 均有本地协议级集成测试。

DeepSeek 在线结果：

1. `deepseek-v4-flash` 普通 completion 返回 `ONLINE_OK`。
2. 真实 SSE tool loop 收到 41 个 delta。
3. 模型调用 `write_file`、`read_file`，两次状态均为 success。
4. 临时文件内容严格为 `KIRAKIRA_TOOL_OK`。
5. session 保存 2 组 tool chain 和完整最终回复。
6. 在线 consolidation 将 10 条消息推进至 `last_consolidated=6`。
7. 生成 2 条可检索长期记忆，并写入 HISTORY。
8. Context 在线冒烟：预检估算 3106 tokens、Provider 实际 3210 tokens、下一轮 history baseline
   22 tokens；选中计划、section breakdown 与 usage 均持久化成功。

API key 仅存在于单次测试进程环境，没有进入代码、配置、文档或 Git。

## 14. 局限与后续方向

当前被动主链路可完整运行，但与 Reference 仍有三类专用差异：

- 没有外部 A2A Peer Agent 的进程冷启动和轮询；本地 subagent 与 MCP 已覆盖大多数委派场景。
- 没有复制 React Dashboard 和 WebSocket transport；现有 Web API 已覆盖 chat/session/memory 管理。
- 没有逐项移植 Akasha graph/default_memory 的 HyDE、query rewrite、procedure conflict 和 profile extraction；当前采用更易维护的 Markdown + typed records + optional hybrid retrieval。

后续若继续演进，优先级应是：把现有 session context trace 暴露为查询/评测 API、建立固定评测集、
A2A peer adapter、对 query rewrite/HyDE 做门控实验，以及独立 Dashboard，而不是继续扩大核心 loop
的职责。语义去重已并入 consolidation 的既有模型调用，不再需要单独增加一次 LLM 往返。

## 15. 简历与面试材料

简历不应描述项目参考或复刻了哪个仓库，而应说明从 MVP 开始解决了哪些 Agent 工程问题。当前版本推荐围绕五条主线组织：Agent Runtime 与并发、ToolRegistry/ToolExecutor、Session/长期记忆、上下文治理与可观测性、真实 Bug 与回归测试。

完整的简历文案、工具执行调用链、记忆/RAG 取舍、面试追问、Bug 闭环，以及 LangSmith 和 100–200 用户后端化完成后的升级写法，见 `docs/RESUME_INTERVIEW_GUIDE.md`。
