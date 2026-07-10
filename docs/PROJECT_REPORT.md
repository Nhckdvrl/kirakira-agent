# Kirakira Agent 被动链路重构报告

## 1. 项目目标

本项目原本是一个非常轻量的同步 agent harness：用户把消息交给 `Agent.run()`，模型决定是否调用工具，工具结果再追加回上下文，直到模型给出最终回复。这个实现适合学习 tool calling 的最小闭环，但离一个真正长期可用的 agent 还差很多关键结构：没有 channel 层、没有消息总线、没有独立 session、没有长期记忆、没有生命周期插件、没有工具 hook，也没有接近 akashic-agent 的被动回复主循环。

这次重构的目标，是参考 `Reference/akashic-agent` 中成熟的被动回复架构，把当前项目改造成一个更完整、可扩展、可实际运行的被动 agent runtime。主动链路、proactive、drift 暂时不复刻；Web、Telegram、QQ 属于被动入口，已经纳入本轮复刻范围。本轮重点是把“用户发一条消息，agent 被动回复”这条主路径搭扎实。

当前实现的主链路如下：

```text
用户 / Channel
        ↓
InboundMessage
        ↓
MessageBus
        ↓
AgentLoop
        ↓
PassiveTurnPipeline
        ↓
BeforeTurn：会话准备 + 记忆检索 + 插件拦截
        ↓
BeforeReasoning：工具上下文同步 + 插件拦截
        ↓
ContextBuilder：prompt 构建 + 当前时间信封 + 记忆注入 + skills
        ↓
DefaultReasoner：LLM 多轮 tool loop
        ↓
ToolExecutor：工具执行 + tool hook 改参 / 阻断 / 观察
        ↓
AfterReasoning：回复改写 + metadata/media 插件处理
        ↓
Session 持久化 + Memory consolidation
        ↓
AfterTurn / TurnCommitted 事件
        ↓
OutboundMessage
        ↓
MessageBus dispatch
        ↓
Channel 回复用户
```

## 2. 与 akashic-agent 的对应关系

这次不是机械复制整个参考项目，而是按“被动链路核心结构”做了精简复刻。保留了关键抽象、数据流和扩展点，去掉了本轮不需要的 dashboard、proactive、复杂 MCP 管理、后台任务调度等部分。

| akashic-agent 概念 | 当前项目实现 | 说明 |
| --- | --- | --- |
| `bus.events.InboundMessage / OutboundMessage` | `kirakira_agent.events` | 定义 channel 与 agent 之间的消息协议 |
| `bus.queue.MessageBus / ChatLane` | `kirakira_agent.bus` | 异步 inbound/outbound 队列，以及同一 chat 的发送顺序控制 |
| `bus.event_bus.EventBus` | `kirakira_agent.event_bus` | lifecycle 事件的 ordered interception 与 fanout observer |
| `session.manager.Session / SessionManager` | `kirakira_agent.session` | session 独立持久化，支持工具链历史重建 |
| `core.memory.runtime / markdown` | `kirakira_agent.memory` | markdown 记忆文件 + 可检索 memory item store |
| `agent.context.ContextBuilder` | `kirakira_agent.context_builder` | 组装系统 prompt、记忆、skills、当前时间信封 |
| `agent.core.passive_turn.PassiveTurnPipeline` | `kirakira_agent.runtime.PassiveTurnPipeline` | 被动回复五阶段主链路 |
| `agent.looping.core.AgentLoop` | `kirakira_agent.runtime.AgentLoop` | 从 bus 消费 inbound message 并驱动 pipeline |
| `agent.tool_hooks.ToolExecutor` | `kirakira_agent.tool_hooks` | 工具调用前后 hook，可改参、阻断、观察 |
| `agent.plugins.PluginManager` | `kirakira_agent.plugins` | 加载本地插件并收集 lifecycle modules/tool hooks |
| OpenAI-compatible provider | `kirakira_agent.models.openai_compatible.OpenAICompatibleClient` | 保留原有模型客户端，并用于 DeepSeek |

## 3. 当前代码结构

核心新增模块如下：

```text
kirakira_agent/
  events.py             # InboundMessage / OutboundMessage
  bus.py                # MessageBus / ChatLane
  event_bus.py          # lifecycle EventBus
  session.py            # Session / SessionManager
  memory.py             # MarkdownMemoryStore / MemoryRuntime
  lifecycle.py          # BeforeTurnCtx / AfterReasoningCtx 等 lifecycle 数据合同
  tool_hooks.py         # ToolExecutor / ToolHook / HookOutcome
  context_builder.py    # prompt 构建与时间信封
  runtime.py            # DefaultReasoner / PassiveTurnPipeline / AgentLoop / CoreRuntime
  plugins.py            # Plugin / PluginManager
  channels/
    contract.py         # Channel / ChannelContext 协议
    host.py             # ChannelHost 启停多个 channel
    base.py             # AttachmentStore / MessageDeduper
    web.py              # Web HTTP channel
    telegram.py         # Telegram Bot API channel
    qq.py               # QQ OneBot/NapCat HTTP channel
  cli.py                # build_runtime 与 CLI local channel
```

原有模块仍然保留：

```text
kirakira_agent/
  agent.py              # 旧同步 Agent 兼容层
  schema.py             # ModelResponse / ToolCall / ToolResult / ToolSpec
  models/
    openai_compatible.py
  tools/
    registry.py
    builtins.py
  skills.py
```

也就是说，旧的 `Agent` 没有被删除。它仍然能通过现有测试，适合最小同步调用；新的完整 runtime 走 `build_runtime()`、`MessageBus` 和 `AgentLoop`。

## 4. 消息层：Inbound / Outbound / MessageBus

### 4.1 InboundMessage

`kirakira_agent.events.InboundMessage` 是 channel 输入 agent 的统一协议：

- `channel`：来源渠道，例如 `cli`
- `sender`：发送者标识
- `chat_id`：会话路由 ID
- `content`：用户文本内容
- `timestamp`：消息时间，默认使用当前本地带时区时间
- `media`：媒体路径或 URL 列表
- `metadata`：扩展信息

`session_key` 默认由 `channel:chat_id` 组成。这样不同渠道、不同聊天窗口之间的历史天然隔离。

### 4.2 OutboundMessage

`kirakira_agent.events.OutboundMessage` 是 agent 回复 channel 的统一协议：

- `channel`
- `chat_id`
- `content`
- `thinking`
- `reply_to`
- `media`
- `metadata`

本轮已经接入 Web、Telegram、QQ 三种被动 channel。它们都遵循同一条原则：

```text
外部平台消息 -> InboundMessage -> MessageBus -> AgentLoop
Agent 回复 -> OutboundMessage -> MessageBus dispatch -> 外部平台 API
```

### 4.3 MessageBus

`kirakira_agent.bus.MessageBus` 负责两个队列：

- `_inbound`：channel → agent
- `_outbound`：agent → channel

关键方法：

- `publish_inbound(msg)`
- `consume_inbound()`
- `complete_inbound(msg)`
- `publish_outbound(msg)`
- `subscribe_outbound(channel, callback)`
- `dispatch_outbound()`

### 4.4 ChatLane

`ChatLane` 是 akashic-agent 里很重要的一个思想：同一个 chat 的被动回复和发送不能互相踩。当前实现保留了基础的 per-chat send coordination，保证同一 `(channel, chat_id)` 下的 outbound dispatch 有顺序。

本轮强审计后还补充了 outbound queue 的 `task_done()` 收尾，后续如果需要对 `_outbound.join()` 做 graceful shutdown，不会因为已 dispatch 的消息未标记完成而卡住。

## 5. AgentLoop：被动主循环入口

`kirakira_agent.runtime.AgentLoop` 是真正消费消息的主循环。

它做的事情很克制：

1. 从 `MessageBus.consume_inbound()` 等待一条 `InboundMessage`
2. 根据 `msg.session_key` 识别会话
3. 创建异步任务执行 `PassiveTurnPipeline.run(msg, key)`
4. 如果 pipeline 抛错，发出降级错误回复
5. 调用 `MessageBus.complete_inbound(msg)`

它不负责 prompt、不负责工具、不负责记忆。这些都下沉到 pipeline 和 reasoner。这样结构更接近 akashic-agent：loop 是主循环，pipeline 才是 turn 处理主干。

## 6. Session：会话持久化与工具链重建

`kirakira_agent.session.SessionManager` 把 session 持久化到：

```text
sessions/{safe_session_key}.json
```

每个 `Session` 包含：

- `key`
- `messages`
- `created_at`
- `updated_at`
- `metadata`
- `last_consolidated`

每轮成功回复后，pipeline 会写入两条消息：

1. 用户消息
2. assistant 消息

assistant 消息不仅保存最终文本，还保存：

- `tools_used`
- `tool_chain`
- `thinking`
- 插件或 pipeline 写入的额外 metadata

`tool_chain` 结构里记录每个工具调用：

- `call_id`
- `name`
- `arguments`
- `result`
- `status`

`Session.get_history()` 会把持久化的工具链重新展开成 OpenAI-compatible messages：

```text
assistant(tool_calls)
tool(result)
assistant(final reply)
```

这样下一轮模型能看到之前工具调用的完整上下文，而不是只看到一句最终回复。这一点是 agent 能长期工作的关键。

## 7. Memory：长期记忆、近期语境与检索

当前记忆系统在 `kirakira_agent.memory`。

初始化时会创建：

```text
memory/MEMORY.md
memory/SELF.md
memory/RECENT_CONTEXT.md
memory/items.json
```

### 7.1 Markdown 记忆文件

`MarkdownMemoryStore` 管理三份 markdown：

- `MEMORY.md`：长期记忆，面向稳定事实、偏好、用户画像
- `SELF.md`：agent 的自我认知或行为设定
- `RECENT_CONTEXT.md`：近期语境摘要，用于下一轮理解最近发生了什么

### 7.2 Memory item store

`items.json` 是结构化 memory item store。每条 `MemoryRecord` 包含：

- `id`
- `content`
- `created_at`
- `source_ref`
- `status`

当前检索使用轻量 lexical scoring：英文/数字 token 与中文连续片段匹配。它不是 akashic-agent 默认 memory engine 那种复杂语义检索，但接口已经按可替换 engine 的方式组织，后续可以升级成 embedding / sqlite / akasha graph。

### 7.3 每轮前检索

在 `PassiveTurnPipeline.run()` 的前置阶段：

1. 从 session 取历史
2. 调 `memory.build_retrieval_block(msg.content)`
3. 把相关 memory item 渲染成 `Retrieved Long-Term Memory` 块
4. 注入 prompt

### 7.4 每轮后 consolidation

每轮回复后：

1. `RECENT_CONTEXT.md` 追加一行近期摘要
2. 如果用户明确说“记住：...”或“以后要记得...”，自动写入 memory item
3. 更新 `session.last_consolidated`

强审计后补充了两条防重复逻辑：

- `memorize()` 对 active 且内容完全一致的记忆直接返回已有 record，不重复写入。
- 如果模型本轮已经显式调用过 `memorize`，consolidation 不再根据同一条用户消息自动提取一份重复记忆。

### 7.5 记忆工具

默认工具集现在包含：

- `memorize(content)`：写入长期记忆
- `recall_memory(query, limit)`：检索长期记忆
- `forget_memory(ids)`：把记忆标记为 forgotten
- `search_messages(query, limit)`：关键词搜索 session 消息
- `fetch_messages(source_ref, context)`：按 source ref 回源上下文

这些工具对应了 akashic-agent prompt 里的历史检索协议与记忆纠错协议的最小可运行版本。

### 7.6 新增被动研究与推送工具

对照 akashic-agent 的非主动工具集，当前项目还补了：

- `list_dir(path)`：列出 workspace 内目录
- `tool_search(query, limit)`：搜索当前可用工具
- `web_fetch(url, max_chars)`：抓取网页并转成可读文本
- `web_search(query, limit)`：返回网页搜索结果标题与 URL
- `message_push(channel, chat_id, message)`：通过 MessageBus 向指定 channel/chat 发送消息

`web_fetch` 默认拒绝 localhost、私有网段、link-local、reserved、multicast、unspecified 地址，避免 agent 工具被提示注入后扫描本机或内网服务。只有在可信本地测试中显式设置 `KIRAKIRA_ALLOW_PRIVATE_WEB_FETCH=true` 才允许访问这些地址。

文件工具统一使用 UTF-8 读写；读取时用 `errors="replace"` 降级处理，避免遇到非 UTF-8 文本直接中断 turn。`bash` 工具补充了危险命令拦截和 timeout 上限。

## 8. ContextBuilder：prompt 构建

`kirakira_agent.context_builder.ContextBuilder` 负责每轮 prompt。

它会组装：

- agent 静态身份
- 行为规范
- 工作区路径
- 当前运行环境
- 当前 session 信息
- `MEMORY.md`
- `SELF.md`
- `RECENT_CONTEXT.md`
- 本轮检索到的 memory block
- 当前激活 skill 内容
- skill catalog
- 插件注入的 system sections
- 本轮额外 hints

当前用户消息会被加上时间信封：

```text
[当前消息时间: ... | request_time=... | 今天=YYYY-MM-DD（周X） | 昨天=... | 明天=... | 相对时间以此为准]
用户原始消息
```

这样模型处理“今天、明天、昨天、刚才”等相对时间时有明确锚点。

## 9. DefaultReasoner：LLM tool loop

`kirakira_agent.runtime.DefaultReasoner` 是模型推理与工具循环层。

每轮执行过程：

1. 构造 `PromptRenderCtx`
2. 经过 `EventBus.emit(PromptRenderCtx)`
3. 运行 prompt-render plugin modules
4. 调 `ContextBuilder.render()` 得到 OpenAI-compatible messages
5. 进入多轮 tool loop

每个 iteration：

1. 构造 `BeforeStepCtx`
2. 经过 event bus 与 before-step plugin modules
3. 如果插件要求 early stop，直接返回
4. 调模型客户端 `complete()`
5. 如果没有 tool calls，返回最终文本
6. 如果有 tool calls，逐个经过 `ToolExecutor`
7. 追加 assistant tool call message
8. 追加 tool result message
9. 构造 `AfterStepCtx` fanout 给观察者
10. 进入下一轮

当前还会发出工具 lifecycle 事件：

- `TurnStarted`
- `ToolCallStarted`
- `ToolCallCompleted`

这些事件可被 channel 或插件观察，用来实现工具轨迹、UI 状态、日志、审计等能力。

最大轮数由 `RuntimeConfig.max_iterations` 控制，默认从环境变量 `AGENT_MAX_ITERATIONS` 读取，不设置则为 10。

## 10. ToolExecutor：工具 hook

`kirakira_agent.tool_hooks.ToolExecutor` 支持三类 hook：

- `pre_tool_use`
- `post_tool_use`
- `post_tool_error`

`pre_tool_use` 可以：

- 允许工具调用
- 修改工具参数
- 拒绝工具调用
- 追加额外提示信息

功能测试里已经验证过：注册一个拒绝 `read_file` 的 hook 后，模型即使请求 `read_file`，实际工具也不会执行，session 里会记录：

```text
status = denied
result = read_file denied by functional smoke hook
```

这对应 akashic-agent 的“插件可以阻断工具”能力。

## 11. PassiveTurnPipeline：被动回复主干

`kirakira_agent.runtime.PassiveTurnPipeline` 是本次复刻最核心的模块。

它的 `run()` 包含完整被动 turn：

### 11.1 BeforeTurn

做这些事：

1. `SessionManager.get_or_create(session_key)`
2. `session.get_history()`
3. memory retrieval
4. skill mention 检测，例如 `$python-coding`
5. 构造 `BeforeTurnCtx`
6. 经过 `EventBus.emit()`
7. 经过 before-turn plugin modules

插件可以在这个阶段：

- 修改 skill_names
- 添加 extra_hints
- 写 extra_metadata
- `abort=True` 直接拦截本轮并返回 `abort_reply`

### 11.2 BeforeReasoning

做这些事：

1. 设置工具上下文：
   - `session_key`
   - `channel`
   - `chat_id`
   - `current_timestamp`
2. 构造 `BeforeReasoningCtx`
3. 经过 event bus 与插件链

插件可以在这个阶段继续补 hints、改 retrieved memory block、或者 abort。

### 11.3 Reasoner

调用 `DefaultReasoner.run_turn()`，进入 prompt render 和 LLM tool loop。

返回 `ReasonerResult`：

- `reply`
- `tools_used`
- `tool_chain`
- `thinking`

### 11.4 AfterReasoning

构造 `AfterReasoningCtx`，允许插件改：

- `reply`
- `media`
- `outbound_metadata`

然后构造 `OutboundMessage`。

### 11.5 Commit + Consolidation

写入 session：

1. user message
2. assistant message

assistant message 会带上完整 `tools_used` 与 `tool_chain`。

然后调用 `memory.consolidate_turn()` 更新近期语境和显式记忆。

### 11.6 AfterTurn

fanout：

- `TurnCommitted`
- `AfterTurnCtx`

然后如果 `dispatch_outbound=True`，调用 `bus.publish_outbound()`。

## 12. PluginManager：插件系统

`kirakira_agent.plugins.PluginManager` 会扫描：

```text
plugins/*/plugin.py
```

插件可以继承 `kirakira_agent.plugins.Plugin`，并实现：

- `initialize()`
- `terminate()`
- `register_tools(registry)`
- `before_turn_modules()`
- `before_reasoning_modules()`
- `prompt_render_modules()`
- `before_step_modules()`
- `after_step_modules()`
- `after_reasoning_modules()`
- `after_turn_modules()`
- `tool_hooks()`

每个插件实例会拿到一个轻量 `context`：

- `event_bus`
- `tool_registry`
- `workspace`
- `session_manager`
- `memory`
- `plugin_dir`

当前插件系统已经覆盖用户要求里的核心能力：

- 拦截命令：before-turn / before-reasoning module 可以 abort
- 注入协议：prompt-render / before-step module 可以加 system sections 或 hints
- 阻断工具：tool hook 的 `decision="deny"`
- 挂载新工具：`register_tools(registry)`

## 13. CLI runtime

`python3 -m kirakira_agent` 现在不再只走旧同步 `Agent`。它会调用：

```python
build_runtime(workdir)
```

构建完整 runtime：

- `MessageBus`
- `EventBus`
- `SessionManager`
- `MemoryRuntime`
- `ToolRegistry`
- `ContextBuilder`
- `DefaultReasoner`
- `PassiveTurnPipeline`
- `AgentLoop`
- `PluginManager`

CLI 输入会被转成：

```python
InboundMessage(
    channel="cli",
    sender="local",
    chat_id="local",
    content=query,
)
```

CLI 支持本地控制命令：

- `/tools`
- `/skills`
- `/memory`
- `/exit`

普通文本都会通过 bus 进入 agent loop。

## 14. Web / Telegram / QQ channel

用户指出“只是不复刻主动链路，不代表不要 Telegram/QQ/Web”。因此当前实现已经补齐这三类被动 channel。它们不参与 proactive，不会主动发起对话；只负责把外部平台的用户消息送入被动主链路，并把 agent 回复发回平台。

### 14.1 Channel 通用协议

channel 协议在 `kirakira_agent.channels.contract`：

```python
class Channel(Protocol):
    name: str
    async def start(self, ctx: ChannelContext) -> None: ...
    async def stop(self) -> None: ...
```

`ChannelContext` 提供：

- `bus`
- `session_manager`
- `event_bus`
- `workspace`
- `log`

`ChannelHost` 负责统一启动和停止多个 channel。

强审计后，CLI 的 runtime 构建和运行已经统一在同一个 asyncio event loop 内完成，避免跨 `asyncio.run()` 携带 bus、queue、channel 状态。`CoreRuntime.stop_background()` 和 REPL 退出路径都会调用插件 `terminate()`。

### 14.2 WebChannel

实现文件：

```text
kirakira_agent/channels/web.py
```

Web channel 使用 Python 标准库 `http.server`，不引入 FastAPI 或 WebSocket 依赖。它提供：

- `GET /`：一个简单可用的网页聊天界面
- `GET /health`：健康检查
- `POST /message`：发送消息并等待本轮 agent 回复

请求格式：

```json
{
  "session_id": "my-session",
  "text": "你好"
}
```

响应格式：

```json
{
  "channel": "web",
  "chat_id": "my-session",
  "session_id": "web:my-session",
  "content": "agent 回复",
  "thinking": "",
  "media": [],
  "metadata": {}
}
```

启动方式：

```bash
python3 -m kirakira_agent --serve --web
```

可配置环境变量：

```text
KIRAKIRA_WEB_ENABLED=true
KIRAKIRA_WEB_HOST=127.0.0.1
KIRAKIRA_WEB_PORT=8765
KIRAKIRA_WEB_CHANNEL=web
```

强审计后，`POST /message` 等待 outbound 的 pending future 会在成功、超时、publish 失败时统一清理，避免长时间服务后同一 chat 的 pending 列表泄漏。

### 14.3 TelegramChannel

实现文件：

```text
kirakira_agent/channels/telegram.py
```

Telegram channel 使用 Telegram Bot API long polling：

- 入站：`getUpdates`
- 出站：`sendMessage`
- 去重：`chat_id:message_id`
- 白名单：用户 id 或 username

启动方式：

```bash
TELEGRAM_BOT_TOKEN=xxx python3 -m kirakira_agent --serve --telegram
```

可配置环境变量：

```text
KIRAKIRA_TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:abcdef
TELEGRAM_ALLOW_FROM=123456789,alice
KIRAKIRA_TELEGRAM_CHANNEL=telegram
```

消息处理：

1. Telegram 用户发文本给 bot
2. channel 转成 `InboundMessage(channel="telegram", chat_id=<telegram chat id>)`
3. agent 被动回复
4. outbound dispatch 调 Telegram `sendMessage`

当前实现以文本消息为主，后续可以继续扩展图片、文件下载、streaming edit、typing indicator 等参考项目里的高级能力。

强审计后，Telegram 出站回复会按 4096 字符分片发送，不再直接截断长回复。

### 14.4 QQChannel

实现文件：

```text
kirakira_agent/channels/qq.py
```

QQ channel 使用 OneBot/NapCat 风格 HTTP webhook + HTTP API：

- 入站：NapCat / OneBot 向本地 webhook POST 事件
- 出站：调用 OneBot HTTP API
  - 私聊：`send_private_msg`
  - 群聊：`send_group_msg`

启动方式：

```bash
python3 -m kirakira_agent --serve --qq
```

默认 webhook：

```text
http://127.0.0.1:8766/qq/webhook
```

可配置环境变量：

```text
KIRAKIRA_QQ_ENABLED=true
QQ_BOT_UIN=12345
ONEBOT_API_BASE_URL=http://127.0.0.1:3000
ONEBOT_ACCESS_TOKEN=
KIRAKIRA_QQ_WEBHOOK_HOST=127.0.0.1
KIRAKIRA_QQ_WEBHOOK_PORT=8766
QQ_ALLOW_FROM=10001,10002
QQ_GROUP_ALLOW=777,888
QQ_REQUIRE_AT=true
KIRAKIRA_QQ_CHANNEL=qq
```

chat_id 约定：

- 私聊：`<user_id>`
- 群聊：`gqq:<group_id>`

群聊过滤：

- `QQ_GROUP_ALLOW` 非空时，只处理白名单群
- `QQ_ALLOW_FROM` 非空时，只处理白名单用户
- `QQ_REQUIRE_AT=true` 且配置了 `QQ_BOT_UIN` 时，群消息必须包含 `[CQ:at,qq=<bot_uin>]`

这对应参考项目里 QQ channel 的核心被动行为：QQ 事件先过滤，再转成 `InboundMessage`，最终通过 bus 回复。

强审计后，QQ 去重键包含 `message_type/group_id/user_id/message_id`，避免不同群里相同 message id 误判重复。OneBot HTTP API 返回体会检查 `status` 与 `retcode`，失败响应会抛出错误并进入 bus 的 outbound retry/logging 路径。

## 15. DeepSeek 接入

`.env` 当前配置为：

```text
OPENAI_COMPATIBLE_BASE_URL=https://api.deepseek.com
OPENAI_COMPATIBLE_API_KEY=<your-deepseek-api-key>
MODEL_ID=deepseek-v4-flash
```

真实 DeepSeek key 只在本地测试命令里通过临时环境变量注入，不写入仓库文件。

模型客户端仍然是原有的 `OpenAICompatibleClient`。它会向：

```text
https://api.deepseek.com/v1/chat/completions
```

发送 OpenAI-compatible chat completions 请求。

对于 `deepseek-v4-*`，现有逻辑会默认加：

```json
{"thinking": {"type": "disabled"}}
```

这点已有测试覆盖。

## 16. Conda 环境

实际找到的 conda / mamba：

```text
/home/xiang/miniconda3/condabin/conda
/home/xiang/.local/bin/micromamba
```

可用环境：

```text
xingshu-vllm   /home/xiang/.conda/envs/xingshu-vllm    Python 3.12.13
cotmad-env     /home/xiang/cotmad-env                  Python 3.12.13
base           /home/xiang/miniconda3                  Python 3.13.13
fgvd           /home/xiang/miniconda3/envs/fgvd         Python 3.12.13
openslime      /home/xiang/miniconda3/envs/openslime    Python 3.12.12
verl-clean     /home/xiang/miniconda3/envs/verl-clean   Python 3.12.0
mamba-bpr      /home/xiang/nlp/.mamba-bpr               Python 3.10.20
```

本轮实际功能测试使用：

```bash
/home/xiang/.conda/envs/xingshu-vllm/bin/python
```

额外确认：

```bash
/home/xiang/miniconda3/envs/fgvd/bin/python -m unittest discover -v
```

也能通过所有测试。

## 17. 验证情况

### 17.1 单元测试

命令：

```bash
python3 -m unittest discover -v
```

结果：

```text
34 tests passed
```

覆盖内容：

- 旧同步 agent 工具循环
- unknown tool 错误处理
- OpenAI-compatible response parsing
- DeepSeek v4 thinking 默认关闭
- 文件工具与 path escape 防护
- `web_fetch` 默认拒绝本地/内网地址，测试环境变量显式放行
- 同步 registry 对 async tool 的兼容处理
- CLI 基础命令
- bus -> loop -> pipeline -> outbound
- session tool_chain 持久化
- 显式记忆 consolidation
- `memorize` 与 consolidation 去重
- before-turn plugin abort
- pre-tool hook denial
- Web channel HTTP 入站和 outbound 回复
- Web channel pending future 超时清理
- QQ OneBot webhook 入站和 outbound API 回复
- QQ OneBot failed retcode/status 检测
- Telegram allow list 逻辑
- Telegram 长回复分片
- `list_dir`
- `web_fetch`
- `tool_search`
- `message_push`
- `TurnStarted`
- `ToolCallStarted`
- `ToolCallCompleted`

### 17.2 Conda 环境下的真实功能测试

命令使用：

```bash
/home/xiang/.conda/envs/xingshu-vllm/bin/python
```

真实跑了这些功能：

1. 从 `.env` 构建完整 runtime
2. 通过 `InboundMessage -> MessageBus -> AgentLoop -> PassiveTurnPipeline -> OutboundMessage` 走完整被动链路
3. 真实调用 DeepSeek `deepseek-v4-flash`
4. 强制模型调用 `read_file`
5. 验证 session 中确实记录：
   - `tools_used=["read_file"]`
   - tool call status 为 `success`
   - tool result 包含测试 probe id
6. 发送“请记住：...”并验证 memory recall 能检索回来
7. 注册一个阻断 `read_file` 的 hook
8. 验证工具调用被拒绝，并在 session 中记录 status 为 `denied`
9. 直接验证 builtin tools：
   - `write_file`
   - `edit_file`
   - `read_file`
   - `bash`

结果：

```text
overall_ok = true
```

### 17.3 Channel 集成测试

新增 channel 测试覆盖：

- `WebChannel`
  - 启动本地 HTTP server
  - `POST /message`
  - 消息进入 agent loop
  - HTTP 响应返回 agent 回复
  - session 持久化为 `web:<session_id>`
- `QQChannel`
  - 启动本地 QQ webhook server
  - 构造 OneBot 群消息事件
  - 校验 `@bot` 群过滤
  - 消息进入 agent loop
  - fake OneBot API 收到 `/send_group_msg`
  - session 持久化为 `qq:gqq:<group_id>`
- `TelegramChannel`
  - 校验 allow list 的 user id 与 username 匹配逻辑

## 18. 当前边界与后续方向

当前实现已经把被动回复链路的架子搭起来了，但还有一些明显后续方向：

1. 记忆检索目前是轻量 lexical matching，后续可以换成 embedding / sqlite FTS / akasha graph。
2. 插件系统已有核心 hook，但还没有 manifest、依赖管理、全局注册表、插件配置模型。
3. MCP/tool discovery 目前没有完整复刻，后续可以按 akashic-agent 的 `tool_search` 和 MCP registry 扩展。
4. Web/Telegram/QQ 已有被动 channel；高级能力如 Telegram markdown entity、图片/文件、streaming edit、QQ 图片下载可以继续细化。
5. 主动链路/proactive/drift 本轮没有实现，后续可以基于当前 session、memory、bus、outbound port 继续加。
6. Session 当前是 JSON 文件，适合轻量开发；如果对话量增长，建议迁移 SQLite。
7. Prompt 规则已覆盖核心行为，但还可以继续迁移 akashic-agent 更细的历史检索协议、记忆纠错协议和 channel-specific rendering policy。

更完整的差异审计见：

```text
docs/DIFFERENCE_AUDIT.md
```

## 19. 总结

当前项目已经从“一个同步 toy agent”升级成“具备完整被动链路的 agent runtime”。它现在有明确的消息协议、异步 bus、session、长期记忆、prompt builder、tool loop、插件 lifecycle、工具 hook、CLI channel、Web channel、Telegram channel、QQ channel，以及真实 DeepSeek 运行验证。

这不是完整 akashic-agent 的一比一复制，但已经精准复刻了用户要求里最重要的被动回复主链路：

```text
Channel
  -> MessageBus
  -> AgentLoop
  -> 记忆检索 + prompt 构建 + LLM tool loop
  -> 工具 / 插件 hook / 记忆工具
  -> session commit + consolidation
  -> Outbound reply
```
