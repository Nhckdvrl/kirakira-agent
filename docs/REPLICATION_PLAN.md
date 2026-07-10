# Kirakira Agent 被动链路复刻计划

## 1. 复刻范围

参考项目 `Reference/akashic-agent` 里有两条重要链路：

1. 被动链路：用户发消息后，agent 回复。
2. 主动链路：agent 根据记忆、状态、传感器、计划等主动触达用户。

本轮只复刻被动链路。主动链路、proactive、drift、dashboard、复杂 MCP 后台服务暂时不做。

注意：Web / Telegram / QQ 属于被动链路入口，不属于主动链路。本轮必须复刻这些 channel。

目标链路：

```text
用户消息 / Channel
        ↓
InboundMessage
        ↓
MessageBus
        ↓
AgentLoop
        ↓
PassiveTurnPipeline
        ↓
BeforeTurn：session + memory retrieval + plugin interception
        ↓
BeforeReasoning：tool context + plugin interception
        ↓
PromptRender：identity + behavior rules + memory + skills + time envelope
        ↓
LLM tool loop
        ↓
ToolExecutor：tool hooks + builtin tools + memory tools
        ↓
AfterReasoning：reply/media/metadata rewrite
        ↓
Session commit + memory consolidation
        ↓
AfterTurn / TurnCommitted
        ↓
OutboundMessage
        ↓
Channel 回复
```

## 2. 设计原则

- 保留旧 `Agent` 同步 API，避免现有测试和轻量调用全部失效。
- 新 runtime 单独建设，不把所有逻辑塞进旧 `agent.py`。
- 先复制 akashic-agent 的结构和数据流，再按当前项目规模精简实现。
- 每个组件只做自己的事：
  - bus 不懂 prompt
  - loop 不懂工具
  - pipeline 不直接发 HTTP
  - reasoner 不直接改 session 文件
  - memory 不依赖具体 channel
- 插件系统先支持核心能力：
  - 拦截 turn
  - 注入 prompt/hints
  - 阻断工具
  - 注册工具
- 记忆系统先可运行，再逐步升级检索质量。

## 3. 任务清单

### 3.1 阅读与架构拆解

- [x] 阅读当前项目结构
- [x] 阅读当前 `Agent` / CLI / tool registry / model client / tests
- [x] 阅读 `Reference/akashic-agent` 的 bus、agent loop、passive pipeline、session、memory、plugin、tool hook
- [x] 明确本轮只复刻被动链路，不复刻 proactive
- [x] 写出当前项目与 akashic-agent 的组件对应关系

### 3.2 消息与总线

- [x] 新增 `kirakira_agent.events`
- [x] 实现 `InboundMessage`
- [x] 实现 `OutboundMessage`
- [x] 新增 `kirakira_agent.bus`
- [x] 实现 `MessageBus`
- [x] 实现 `ChatLane`
- [x] 支持 channel 订阅 outbound callback
- [x] 支持 inbound 完成通知

### 3.3 Lifecycle EventBus

- [x] 新增 `kirakira_agent.event_bus`
- [x] 实现 `EventBus.on()`
- [x] 实现 ordered `emit()`
- [x] 实现 observer `observe()`
- [x] 实现并发 observer `fanout()`
- [x] 让 lifecycle ctx 能被插件或 handler 修改后继续传递

### 3.4 Session 系统

- [x] 新增 `kirakira_agent.session`
- [x] 实现 `Session`
- [x] 实现 `SessionManager`
- [x] session 按 `channel:chat_id` 隔离
- [x] session 持久化到 `sessions/*.json`
- [x] assistant turn 保存 `tools_used`
- [x] assistant turn 保存完整 `tool_chain`
- [x] `get_history()` 能重建 OpenAI-compatible tool call history
- [x] 支持 `search_messages()`
- [x] 支持 `fetch_messages()`

### 3.5 记忆系统

- [x] 新增 `kirakira_agent.memory`
- [x] 初始化 `memory/MEMORY.md`
- [x] 初始化 `memory/SELF.md`
- [x] 初始化 `memory/RECENT_CONTEXT.md`
- [x] 支持结构化 `memory/items.json`
- [x] 实现 `memorize()`
- [x] 实现 `recall()`
- [x] 实现 `forget()`
- [x] 实现 `build_retrieval_block()`
- [x] 实现每轮后 `consolidate_turn()`
- [x] 显式识别“记住：...”写入长期记忆
- [x] 注册记忆工具：
  - [x] `memorize`
  - [x] `recall_memory`
  - [x] `forget_memory`
  - [x] `search_messages`
  - [x] `fetch_messages`

### 3.6 Prompt 构建

- [x] 新增 `kirakira_agent.context_builder`
- [x] 注入 agent 身份与行为规则
- [x] 注入 workspace 路径
- [x] 注入当前 session 信息
- [x] 注入长期记忆
- [x] 注入 self model
- [x] 注入 recent context
- [x] 注入 retrieved memory block
- [x] 注入 skills catalog
- [x] 支持 `$skill` 激活内容
- [x] 给当前用户消息加时间信封
- [x] 支持插件追加 system sections 与 hints

### 3.7 工具系统

- [x] 扩展 `ToolRegistry`
- [x] 支持 `set_context()`
- [x] 支持 `execute_async()`
- [x] 支持 `unregister()`
- [x] 保留旧同步 `execute()`
- [x] 默认工具保留：
  - [x] `bash`
  - [x] `read_file`
  - [x] `write_file`
  - [x] `edit_file`
  - [x] `load_skill`
  - [x] `compact`
- [x] 默认工具新增 memory/history tools

### 3.8 Tool Hook

- [x] 新增 `kirakira_agent.tool_hooks`
- [x] 实现 `ToolExecutionRequest`
- [x] 实现 `HookContext`
- [x] 实现 `HookOutcome`
- [x] 实现 `ToolExecutor`
- [x] 支持 `pre_tool_use`
- [x] 支持 `post_tool_use`
- [x] 支持 `post_tool_error`
- [x] 支持 hook 改参
- [x] 支持 hook deny
- [x] 支持 hook 附加 extra message

### 3.9 Runtime / Reasoner / Pipeline

- [x] 新增 `kirakira_agent.runtime`
- [x] 实现 `RuntimeConfig`
- [x] 实现 `ReasonerResult`
- [x] 实现 `DefaultReasoner`
- [x] 实现 prompt-render lifecycle
- [x] 实现 before-step lifecycle
- [x] 实现 after-step lifecycle
- [x] 实现多轮 LLM tool loop
- [x] 工具调用走 `ToolExecutor`
- [x] 工具结果追加回 messages
- [x] 达到最终回复时停止
- [x] 达到最大 iteration 时给出阶段性回复
- [x] 实现 `PassiveTurnPipeline`
- [x] 实现 BeforeTurn
- [x] 实现 BeforeReasoning
- [x] 实现 AfterReasoning
- [x] 实现 AfterTurn
- [x] 实现 session commit
- [x] 实现 memory consolidation
- [x] 实现 outbound publish
- [x] 实现 `AgentLoop`
- [x] 实现 `CoreRuntime`

### 3.10 插件系统

- [x] 新增 `kirakira_agent.plugins`
- [x] 定义 `Plugin` 基类
- [x] 实现 `PluginManager`
- [x] 扫描 `plugins/*/plugin.py`
- [x] 支持 `initialize()`
- [x] 支持 `terminate()`
- [x] 支持插件注册工具
- [x] 支持 before-turn modules
- [x] 支持 before-reasoning modules
- [x] 支持 prompt-render modules
- [x] 支持 before-step modules
- [x] 支持 after-step modules
- [x] 支持 after-reasoning modules
- [x] 支持 after-turn modules
- [x] 支持 tool hooks

### 3.11 CLI 接入

- [x] 修改 `kirakira_agent.cli`
- [x] 新增 `build_runtime(workdir)`
- [x] CLI 使用完整 runtime
- [x] CLI 输入转为 `InboundMessage(channel="cli")`
- [x] CLI 订阅 outbound
- [x] 支持 `/tools`
- [x] 支持 `/skills`
- [x] 支持 `/memory`
- [x] 支持 `/exit`
- [x] 保留旧 `build_agent()` 和 `repl()` 兼容逻辑

### 3.12 Web / Telegram / QQ Channel

- [x] 新增 `kirakira_agent.channels` 包
- [x] 定义 `Channel` / `ChannelContext`
- [x] 实现 `ChannelHost`
- [x] 实现通用 `AttachmentStore`
- [x] 实现通用 `MessageDeduper`
- [x] 实现 `WebChannel`
  - [x] `GET /`
  - [x] `GET /health`
  - [x] `POST /message`
  - [x] HTTP 请求进入 `MessageBus`
  - [x] 等待同 chat outbound 并返回 JSON
- [x] 实现 `TelegramChannel`
  - [x] Telegram Bot API `getUpdates`
  - [x] Telegram Bot API `sendMessage`
  - [x] allow list
  - [x] 消息去重
  - [x] 入站转 `InboundMessage`
  - [x] 出站转 Telegram API
- [x] 实现 `QQChannel`
  - [x] OneBot/NapCat HTTP webhook
  - [x] OneBot `send_private_msg`
  - [x] OneBot `send_group_msg`
  - [x] 私聊 chat_id
  - [x] 群聊 `gqq:<group_id>` chat_id
  - [x] 群白名单
  - [x] 用户白名单
  - [x] `@bot` 过滤
- [x] CLI 增加 `--serve`
- [x] CLI 增加 `--web`
- [x] CLI 增加 `--telegram`
- [x] CLI 增加 `--qq`
- [x] 支持从环境变量启用 channel

### 3.13 DeepSeek 接入

- [x] `.env` 使用 DeepSeek OpenAI-compatible endpoint
- [x] `MODEL_ID=deepseek-v4-flash`
- [x] 本地写入用户提供的 DeepSeek API key 用于测试
- [x] 上传前恢复 `.env` 占位 key，避免泄露
- [x] 保留 `OpenAICompatibleClient`
- [x] DeepSeek v4 默认关闭 thinking
- [x] 真实调用 DeepSeek smoke test

### 3.14 测试

- [x] 原有测试继续通过
- [x] 新增 runtime 测试
- [x] 测试 bus -> loop -> pipeline -> outbound
- [x] 测试工具链 session 持久化
- [x] 测试显式记忆 consolidation
- [x] 测试 before-turn plugin abort
- [x] 测试 tool hook denial
- [x] 新增 channel 测试
- [x] 测试 Web HTTP channel
- [x] 测试 QQ OneBot webhook channel
- [x] 测试 Telegram allow list
- [x] 用 conda 环境跑单元测试
- [x] 用 conda 环境跑真实 agent 功能测试

## 4. 已验证命令

系统 Python 单测：

```bash
python3 -m unittest discover -v
```

Conda 单测：

```bash
/home/xiang/.conda/envs/xingshu-vllm/bin/python -m unittest discover -v
/home/xiang/miniconda3/envs/fgvd/bin/python -m unittest discover -v
```

Conda 真实功能测试使用：

```bash
/home/xiang/.conda/envs/xingshu-vllm/bin/python
```

验证过：

- runtime 构建
- DeepSeek 真实回复
- DeepSeek 真实 tool loop
- `read_file` 工具调用
- session 中 `tools_used` 与 `tool_chain`
- memory 写入与检索
- tool hook 阻断
- builtin tools 直接执行
- Web 被动 channel
- QQ 被动 channel
- Telegram channel 基础过滤逻辑

## 5. 当前结论

本轮复刻已经完成被动回复链路的核心结构。当前项目已经具备：

- channel/message 抽象
- bus
- agent loop
- passive turn pipeline
- session 持久化
- 记忆系统
- prompt 构建
- LLM tool loop
- 工具系统
- 插件 lifecycle
- tool hook
- DeepSeek 接入
- conda 环境真实功能验证

后续可以在这个基础上继续补：

- Web/Telegram/QQ channel
- MCP/tool search
- 更强记忆引擎
- 插件 manifest/config
- proactive/drift 主动链路
