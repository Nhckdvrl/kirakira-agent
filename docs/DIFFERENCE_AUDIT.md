# Kirakira Agent 与 akashic-agent 非主动链路差异审计

## 1. 审计口径

用户要求只排除“主动链路”，也就是 proactive / drift / 自主后台触达用户这类能力。Web、Telegram、QQ 属于被动入口，不应排除。

本审计按以下分类：

- **已覆盖**：当前项目已有同等或接近能力。
- **已补齐**：本轮根据差异新增。
- **简化覆盖**：当前有可运行版本，但比 akashic-agent 简化。
- **仍缺失但非主动**：不属于主动链路，后续应继续补。
- **主动链路相关，暂不做**：符合用户指定的排除范围。

## 2. 已覆盖 / 已补齐

### 2.1 被动主链路

参考项目主链路：

```text
Channel -> MessageBus -> AgentLoop -> PassiveTurnPipeline -> OutboundMessage
```

当前项目对应：

```text
kirakira_agent.events
kirakira_agent.bus
kirakira_agent.runtime.AgentLoop
kirakira_agent.runtime.PassiveTurnPipeline
```

状态：**已覆盖**。

### 2.2 Web / Telegram / QQ Channel

参考项目：

```text
infra/channels/web_chat_channel.py
infra/channels/telegram_channel.py
infra/channels/qq_channel.py
```

当前项目：

```text
kirakira_agent/channels/web.py
kirakira_agent/channels/telegram.py
kirakira_agent/channels/qq.py
```

状态：**已补齐**。

差异：

- Web：当前用标准库 HTTP server；参考项目使用 FastAPI WebSocket 和 dashboard/chat API。
- Telegram：当前用 Bot API long polling；参考项目使用 `python-telegram-bot`，支持复杂 markdown entity、streaming/live edit、图片和文件。
- QQ：当前用 OneBot/NapCat HTTP webhook + HTTP API；参考项目使用 NcatBot SDK，并有更复杂的 trace、图片下载、群过滤和 NapCat runtime 管理。

当前版本已经可以作为被动入口跑通，但高级体验仍可继续增强。

### 2.3 Session 持久化

当前项目状态：**简化覆盖**。

当前支持：

- session key 隔离
- JSON 持久化
- `tools_used`
- `tool_chain`
- OpenAI-compatible history 重建
- `search_messages`
- `fetch_messages`

参考项目额外支持：

- SQLite store
- FTS5 trigram 搜索
- dashboard 查询
- next_seq/message id 更完整
- metadata 刷新策略

### 2.4 记忆系统

当前项目状态：**简化覆盖**。

当前支持：

- markdown memory files
- `items.json` memory item store
- `memorize`
- `recall_memory`
- `forget_memory`
- 每轮 recent context consolidation
- 显式“记住：...”提取

参考项目额外支持：

- memory plugin protocol
- default memory engine
- akasha graph memory
- 更复杂的检索 trace
- dashboard 管理
- 更强的纠错、时间过滤、source evidence

### 2.5 工具系统

参考项目常见被动工具包括：

```text
read_file
write_file
edit_file
list_dir
shell
web_fetch
web_search
load_skill
memorize
recall_memory
forget_memory
search_messages
fetch_messages
message_push
tool_search
```

当前项目已支持：

```text
bash
read_file
write_file
edit_file
list_dir
load_skill
compact
memorize
recall_memory
forget_memory
search_messages
fetch_messages
tool_search
web_fetch
web_search
message_push
```

状态：**已补齐一批高价值非主动工具**。

### 2.6 Lifecycle 事件

当前项目已支持：

- `BeforeTurnCtx`
- `BeforeReasoningCtx`
- `PromptRenderCtx`
- `BeforeStepCtx`
- `AfterStepCtx`
- `AfterReasoningCtx`
- `AfterTurnCtx`
- `TurnCommitted`
- `TurnStarted`
- `ToolCallStarted`
- `ToolCallCompleted`

状态：**主要 lifecycle 已覆盖**。

仍缺：

- `StreamDeltaReady`，因为当前模型客户端不是 streaming client。

### 2.7 Tool Hook

当前项目状态：**简化覆盖**。

当前支持：

- `pre_tool_use`
- `post_tool_use`
- `post_tool_error`
- 改参
- deny
- extra message

参考项目额外支持更完整的 trace item 与 preflight。

### 2.8 插件系统

当前项目状态：**简化覆盖**。

当前支持：

- `plugins/*/plugin.py`
- `initialize`
- `terminate`
- 注册工具
- lifecycle modules
- tool hooks

参考项目额外支持：

- manifest / aka descriptor
- plugin config model
- global registry
- marketplace/cache source resolver
- plugin skill link
- plugin jobs
- dashboard 插件面板

## 3. 仍缺失但不属于主动链路

这些不是 proactive/drift，但体量较大，建议后续继续补。

### 3.1 MCP

参考项目：

```text
agent/mcp/*
bootstrap/toolsets/mcp.py
```

当前项目未实现 MCP client、MCP server registry、MCP tool 映射和 MCP tool discovery。

### 3.2 Spawn / subagent

参考项目：

```text
agent/subagent.py
agent/background/*
agent/tools/spawn.py
```

spawn 是被动 turn 中由用户或主模型触发的子任务，不等同于主动链路。当前项目未实现。建议后续补一个精简版同步 spawn，再考虑后台 job。

### 3.3 Scheduler

参考项目：

```text
agent/scheduler.py
agent/tools/schedule.py
```

scheduler 是用户触发的延迟 side effect，边界上接近主动触达。当前未实现，建议在用户明确需要提醒工具时再补。

### 3.4 更完整文件工具

参考项目 filesystem 工具支持编码检测、图片 inline、行号窗口、CRLF/BOM、diff preview、mutation lock、binary detection。当前项目是可用但简化版本。

### 3.5 Streaming

参考项目支持 `StreamDeltaReady`、Telegram live edit、WebSocket incremental update。当前项目没有 streaming provider，因此 channel 返回最终回复。

### 3.6 SQLite session store / FTS

当前 JSON session 可用，但长期对话多了以后建议迁移 SQLite + FTS5 trigram。

### 3.7 插件 manifest/config

当前插件只靠 `plugins/*/plugin.py` 加载。参考项目的 manifest、配置、启停、全局 registry、skill links 仍未实现。

## 4. 主动链路相关，暂不做

这些符合用户明确排除范围：

- `proactive_v2/*`
- `agent/core/proactive_turn.py`
- `agent/core/drift_turn.py`
- proactive gateway / sensor / judge / energy
- drift tools/state
- memory optimizer 里主动维护 recent context 的后台行为

注意：部分模块名称里带 background，但不一定都是 proactive。例如 spawn background 是被动 turn 发起的子任务，不应和 proactive 混为一谈。

## 5. 本轮新增验证

命令：

```bash
/home/xiang/.conda/envs/xingshu-vllm/bin/python -m unittest discover -v
```

结果：

```text
34 tests passed
```

新增覆盖：

- `list_dir`
- `tool_search`
- `web_fetch`
- `message_push`
- `TurnStarted`
- `ToolCallStarted`
- `ToolCallCompleted`
- `web_fetch` 默认拒绝本地/内网地址
- registry 同步执行 async tool
- `memorize` 与 consolidation 去重
- Telegram 长回复分片
- QQ OneBot failed retcode/status
- Web channel pending future 清理

## 6. 下一步建议

如果继续按“非主动链路尽量补齐”的方向走，建议优先级如下：

1. SQLite session store + FTS 搜索。
2. 更完整 filesystem 工具。
3. MCP client/registry。
4. 精简 spawn/subagent。
5. 插件 manifest/config。
6. streaming provider + Web/Telegram streaming。
