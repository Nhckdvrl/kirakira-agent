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
       ContextBuilder
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
  lifecycle.py              7 phase ctx 与 turn/tool/stream 事件
  event_bus.py              ordered interception 和 fanout observer
  session.py                JSON session、FTS 索引、history reconstruction
  memory.py                 Markdown/typed memory 与后台 consolidation
  embeddings.py             OpenAI-compatible embedding client
  context_builder.py        system prompt、时间、记忆、skills、附件
  tool_hooks.py             pre/post/error 工具 hook executor
  plugins.py                插件加载、回滚、配置、KV 和管理工具
  plugin_manifest.py        .aka-plugin descriptor 安全解析
  plugin_decorators.py      tool/hook/phase decorators
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
    registry.py             server/tool 动态注册和持久化
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

1. 等待同 session 上一轮 memory consolidation。
2. 从 Session 重建模型历史。
3. 执行长期记忆召回。
4. 识别 `$skill-name`。
5. 依次执行 EventBus handler 和插件 BeforeTurn modules。

插件可以修改历史、追加记忆候选、激活 skill、追加 hint，或直接返回回复终止本轮。

### 4.4 BeforeReasoning 与 PromptRender

Pipeline 使用 `ContextVar` 绑定当前 `session_key/channel/chat_id/timestamp`。这个上下文只在当前 async task 中可见，避免并发 turn 的工具路由串线。

`ContextBuilder` 组装：

- Agent 身份和行为规则。
- workspace、memory、self、recent context 路径和内容。
- 当前 channel/chat。
- 带时区的今天/昨天/明天时间信封。
- 检索得到的长期记忆。
- skills catalog 和本轮激活的 skill 全文。
- 插件 top/bottom sections 与 turn hints。
- 用户附件路径和视觉工具提示。

`PromptRenderCtx` 允许插件在最终请求模型前修改 messages、system prompt、可见工具和 hints。

## 5. LLM Tool Loop

`DefaultReasoner.run_turn()` 是被动回复的推理核心。

每轮步骤：

1. 根据 always-on、deferred LRU 和 disabled set 计算工具 schema。
2. 调用 OpenAI-compatible client；启用 stream 时解析 SSE。
3. 将正文和 reasoning 增量发布为 `StreamDeltaReady`。
4. 没有 tool call 时返回最终回复。
5. 有 tool call 时写入 assistant tool-call message。
6. 对每个 call 发出 BeforeStep、ToolCallStarted。
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

1. 中文 bigram、英文 token、substring 计算词法分。
2. 若配置 embedding，则请求 `/embeddings` 并计算 cosine。
3. 按 0.75 semantic + 0.25 lexical 混合。
4. 叠加 exact substring 与 reinforcement 权重。
5. 应用 type/since/until 过滤。
6. 生成带 id/type/source 的 retrieved block 注入 prompt。

回复后先同步更新 RECENT_CONTEXT/HISTORY，再调度后台 LLM consolidation。达到窗口后，模型只抽取用户明确表达的稳定身份、偏好、流程和事件；assistant 建议不会作为用户事实。结果使用 JSON parser、source_ref 和 dedup 写入。

## 9. 插件与 MCP

插件扫描 `<workspace>/.kirakira/plugins` 和 `<workspace>/plugins`。兼容 `.aka-plugin/plugin.json`，可声明：

- lifecycle class/entry。
- skills roots。
- MCP servers。
- config schema 和本地覆盖配置。

`PluginContext` 提供 workspace、session、memory、event bus、tool registry、独立 data dir 和原子 KV。插件可通过类方法或 decorators 注册工具、hook 和 phase module。

MCP client 使用 Content-Length framed stdio JSON-RPC：

- `initialize` handshake。
- request id -> Future 并发关联。
- `tools/list` 和 `tools/call`。
- timeout、server error、stderr drain、异常退出。

MCP 远端工具默认 deferred，避免一次将大量 schema 塞入 prompt。模型用 `tool_search select:name` 解锁后才可调用。

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

[agent]
max_iterations = 40
max_tokens = 8192

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
- 83 项 unittest 全部通过。
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

API key 仅存在于单次测试进程环境，没有进入代码、配置、文档或 Git。

## 14. 局限与后续方向

当前被动主链路可完整运行，但与 Reference 仍有三类专用差异：

- 没有外部 A2A Peer Agent 的进程冷启动和轮询；本地 subagent 与 MCP 已覆盖大多数委派场景。
- 没有复制 React Dashboard 和 WebSocket transport；现有 Web API 已覆盖 chat/session/memory 管理。
- 没有逐项移植 Akasha graph/default_memory 的 HyDE、query rewrite、procedure conflict 和 profile extraction；当前采用更易维护的 Markdown + typed records + optional hybrid retrieval。

后续若继续演进，优先级应是：可观测性指标和 trace API、A2A peer adapter、语义去重与 query rewrite、独立 Dashboard，而不是继续扩大核心 loop 的职责。

## 15. 简历项目介绍

### 精简版

**Kirakira Agent｜多渠道可扩展 AI Agent Runtime**

参考 akashic-agent 重构并实现完整被动式 Agent 架构，打通 Web、Telegram、QQ/OneBot、CLI 到 MessageBus、会话隔离、长期记忆、流式 LLM Tool Loop、插件 Hook、MCP 和子 Agent 的端到端链路。实现跨会话并发/同会话串行、SSE 工具调用、DeepSeek reasoning 历史回放、FTS5 会话检索、语义+词法混合记忆、后台 consolidation、工具安全边界及 graceful shutdown；在 Python 3.12 conda 环境完成 83 项自动化测试，并使用 `deepseek-v4-flash` 在线验证真实工具循环与记忆抽取。

### 要点版

- 设计异步 MessageBus 与 session-aware AgentLoop，实现跨会话并发、同会话串行、消息保序、turn 中断和可恢复工具链持久化。
- 实现 OpenAI-compatible SSE Tool Loop，支持 fragmented tool calls、DeepSeek `reasoning_content` 回放、schema 校验、重复调用保护、上下文裁剪与故障重试。
- 构建 Markdown + JSON + SQLite FTS5 长期记忆体系，支持 source evidence、幂等强化、遗忘/撤销、可选 embedding 混合检索和异步 LLM consolidation。
- 设计插件/工具扩展层，支持 7 阶段生命周期、pre/post/error Hook、descriptor/config/KV、stdio MCP 动态工具、deferred discovery 和隔离回滚。
- 接入 Web、Telegram、QQ/OneBot 与 CLI，完善附件、流式编辑、群策略、鉴权、并发 request correlation 和优雅关机。

建议在简历中同时附上 GitHub 链接，并将“83 项测试”和“DeepSeek 在线 tool loop”保留为可量化证据。
