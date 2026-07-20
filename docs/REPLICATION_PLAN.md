# Kirakira Agent 被动链路复刻计划与完成清单

## 1. 范围

本项目以 `Reference/akashic-agent` 为结构与行为参考，只排除自主主动链路：

- 不做 `proactive_v2` 的传感器、energy、judge、presence、source polling。
- 不做 drift 空闲自治和“无人请求时自行决定触达用户”。
- 保留所有由用户消息直接触发的能力，包括 Web、Telegram、QQ、定时消息、后台 shell、子 Agent 和显式 `message_push`。

目标被动链路：

```text
Channel -> InboundMessage -> MessageBus -> AgentLoop
        -> memory retrieval -> lifecycle phases -> prompt render
        -> named PromptBlocks + dynamic Context Frame + provider preflight
        -> streaming LLM tool loop -> hooks/tools/MCP/plugins
        -> session commit -> OutboundMessage -> Channel
        -> background memory consolidation
```

## 2. 完成清单

### 消息、并发与生命周期

- [x] 统一 `InboundMessage` / `OutboundMessage` 合同。
- [x] inbound/outbound 异步队列和 channel 订阅。
- [x] 同 chat 发送顺序、不同 chat 并发。
- [x] 同 session 串行 turn、不同 session 并行。
- [x] `/stop` 取消运行中 turn，并持久化 partial tool chain。
- [x] queue `task_done`、drain、unsubscribe 和 graceful shutdown。
- [x] 7 个 phase 上下文和 turn/tool/stream 生命周期事件。
- [x] handler 有序执行、observer fanout、异步事件关机等待。

### Reasoner 与模型协议

- [x] OpenAI-compatible 普通响应与 SSE streaming。
- [x] fragmented tool call 参数重组。
- [x] DeepSeek `reasoning_content` 透传和历史回放。
- [x] tool schema 校验、未知/隐藏工具拒绝。
- [x] 重复工具调用保护和最大迭代阶段总结。
- [x] 空回复重试、模型超时、429/5xx 重试。
- [x] context length/content safety 分级裁剪重试。
- [x] 具名 PromptBlock、稳定 system/dynamic Context Frame 分离与静态 block cache。
- [x] system/messages/tool schema/image 的统一输入估算与 Provider 请求前预检。
- [x] `ContextPrepared` / `ContextBudgetUpdated`、section breakdown、retry trace 和实际模型 usage。
- [x] deferred tools、`tool_search` 解锁和 session LRU。

### Session 与记忆

- [x] JSON canonical session 存储和原子写入。
- [x] 哈希安全文件名和旧文件迁移。
- [x] tool/reasoning history 无损重建。
- [x] 历史从 `last_consolidated` 开始；过量未归档消息先强制 consolidation，失败时明确阻断。
- [x] 工具长结果头尾保留、总行数和省略量标记；历史裁剪保持 user/tool-call 边界。
- [x] SQLite FTS5 trigram 消息索引与 JSON 回源。
- [x] session list/search/fetch/delete。
- [x] Markdown 长期记忆和类型化 `items.json`。
- [x] exact dedup、reinforcement、source_ref、遗忘。
- [x] session 删除时撤销源自该 session 的记忆。
- [x] 中文 bigram/英文 token 词法检索。
- [x] 可选 embedding 语义+词法混合检索。
- [x] memory type/time filter。
- [x] 回复后异步 LLM consolidation；同 session 下一轮等待收口。
- [x] MEMORY/SELF/RECENT_CONTEXT/HISTORY/PENDING 文件初始化。
- [x] 完整 `RetrievalRequest` 携带 history、session/channel metadata 与时间。

### 工具、MCP、插件与子任务

- [x] 文件读写、歧义编辑、二进制检测和路径越界防护。
- [x] shell 超时/取消进程组清理。
- [x] 后台 shell、`task_output`、`task_stop` 和 runtime 回收。
- [x] `web_fetch` SSRF、重定向、类型和大小限制。
- [x] web search、vision、message push。
- [x] memory/history 工具。
- [x] stdio MCP JSON-RPC 并发 client。
- [x] MCP initialize/list/call；声明式 `mcp/servers/*.toml` 热重载与整批候选语义。
- [x] 运行时能力快照与每 turn 租约：热重载不影响在途 turn，旧代际租约排空后才回收。
- [x] 插件程序化能力声明（`plugin.py`）、启停清单、config、KV、skills、MCP、channels。
- [x] `@tool`、`@on_tool_pre` 和 7 phase decorators。
- [x] 插件加载失败回滚、错误隔离和反向 terminate。
- [x] plugin install/list/doctor，安装后重启生效。
- [x] 同步/后台 subagent，独立 session 和 profile 权限。
- [x] 后台 subagent 并发限制、list/cancel、完成事件回注。
- [x] 用户显式 schedule/list/cancel 持久化调度。

### Channels

- [x] Textual TUI + streaming Plain CLI；默认新建空 Session，`/sessions` 恢复历史。
- [x] Web chat、并发 request correlation 和 unsolicited event long poll。
- [x] Web session/memory 管理 API。
- [x] Telegram long polling、allow list、附件、分片、429 retry。
- [x] Telegram streaming live edit 和出站文件。
- [x] QQ/OneBot webhook、鉴权、私聊/群聊策略和 @ 过滤。
- [x] QQ 图片/文件收发、发送者身份、retcode 校验。
- [x] ChannelHost 部分启动失败回滚和反向关机。

### 质量与交付

- [x] `Reference/`、本地配置、运行数据和密钥进入 `.gitignore`。
- [x] 使用现有 conda 环境运行 compileall 和 186 项自动化测试（183 通过、3 项条件跳过）。
- [x] 使用 `deepseek-v4-flash` 在线验证普通响应。
- [x] 在线验证 SSE + write/read tool loop + session tool chain。
- [x] 在线验证后台 LLM memory consolidation。
- [x] 在线验证 context 预检估算、Provider usage 与下一轮 baseline trace。
- [x] 中文 README、差异审计和项目报告重写。
- [ ] 可选增强：A2A Peer Agent 冷启动/轮询。
- [ ] 可选增强：独立 React Dashboard 和 WebSocket transport。
- [ ] 可选增强：Akasha graph/default_memory 的专用检索算法移植。

## 3. 验收标准

1. 被动入口全部收敛到同一个 MessageBus 和 AgentLoop，不在 channel 内复制 agent 逻辑。
2. 同 session 不并发写历史，不同 session 不互相阻塞。
3. tool call 的参数、结果、错误、thinking 可完整持久化并回放。
4. 插件能够改写/阻断 turn 与工具，坏插件不能阻断其他插件或核心启动。
5. MCP、后台进程、channel、memory worker 在关机时没有悬挂任务。
6. 长期记忆可写、可检索、可遗忘、可随 session 删除撤销。
7. Web、Telegram、QQ 的入站和出站均有自动化集成测试。
8. DeepSeek 在线测试至少覆盖一次真实 tool loop 和一次 consolidation。

以上标准当前均已满足；未勾选项是 Reference 的可选专用子系统，不是被动主链路阻塞项。
