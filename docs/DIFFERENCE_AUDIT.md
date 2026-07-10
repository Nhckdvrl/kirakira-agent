# Kirakira Agent 与 akashic-agent 非主动链路差异审计

## 1. 审计结论

审计对象为本仓库与 `Reference/akashic-agent` 当前源码。排除项只包括自主主动链路：`proactive_v2`、drift、sensor、energy、judge、presence，以及无人请求时的自主触达。Web、Telegram、QQ、用户创建的定时任务、用户 turn 派生的后台任务仍属于被动链路范围。

当前 Kirakira 已覆盖 Reference 被动主链路的关键行为：统一 Channel、MessageBus、会话并发控制、生命周期 phase、streaming tool loop、session、长期记忆、插件、MCP、技能、后台 shell、subagent、调度和 graceful shutdown。它不是 Reference 的逐文件拷贝，部分专用子系统使用更轻的实现，差异见第 4 节。

## 2. 逐模块映射

| Reference | Kirakira | 状态 |
| --- | --- | --- |
| `bus/events.py` | `kirakira_agent/events.py` | 已覆盖 |
| `bus/queue.py` | `kirakira_agent/bus.py` | 已覆盖并测试并发/顺序 |
| `bus/event_bus.py`、`events_lifecycle.py` | `event_bus.py`、`lifecycle.py` | 已覆盖主要被动事件 |
| `agent/looping/*` | `runtime.AgentLoop` | 已覆盖被动消费、串行化、中断 |
| `agent/core/passive_turn.py` | `runtime.DefaultReasoner` | 已覆盖 streaming tool loop 与 retry |
| `agent/turns/*`、`lifecycle/phases/*` | `PassiveTurnPipeline` + phase ctx | 已覆盖 turn 处理阶段 |
| `agent/prompting/*` | `context_builder.py` + reasoner trim | 行为覆盖，结构更轻 |
| `session/manager.py`、`store.py` | `session.py` | JSON canonical + SQLite FTS |
| `core/memory/*`、`memory2/*` | `memory.py`、`embeddings.py` | 核心行为覆盖，算法简化 |
| `agent/tools/*` | `tools/builtins.py` | 常用被动工具已覆盖 |
| `agent/tool_hooks/*` | `tool_hooks.py` | pre/post/error hook 已覆盖 |
| `agent/mcp/*` | `mcp/client.py`、`mcp/registry.py` | stdio MCP 已覆盖 |
| `agent/plugins/*` | `plugins.py`、`plugin_manifest.py`、`plugin_decorators.py` | 核心插件合同已覆盖 |
| `agent/background/*`、`tools/spawn.py` | `subagent.py` | inline/background 与管理面已覆盖 |
| `agent/scheduler.py`、`tools/schedule.py` | `scheduler.py` | 用户显式调度已覆盖 |
| `infra/channels/web_chat_channel.py` | `channels/web.py` | 已覆盖，transport 不同 |
| `infra/channels/telegram_channel.py` | `channels/telegram.py` | 已覆盖 Bot API 行为 |
| `infra/channels/qq_channel.py` | `channels/qq.py` | 已覆盖 OneBot HTTP 行为 |
| `bootstrap/channel_host.py` | `channels/host.py` | 已覆盖启停和失败回滚 |
| `bootstrap/app.py`、`wiring.py` | `cli.build_runtime`、`CoreRuntime` | 已覆盖核心装配 |

## 3. 已补齐的高风险差异

### 3.1 MessageBus 与 AgentLoop

- inbound 消息入队时登记 passive pending，处理完成必定 `task_done`。
- 同 session 通过独立 lock 串行；不同 session 的 turn task 并发。
- outbound 为每个 `(channel, chat_id)` 分配 ticket，同 chat 保序、跨 chat 并发。
- subscriber 失败会重试一次，单个 callback 失败不破坏 dispatch loop。
- `/stop` 取消 active task；取消时保存用户消息、已完成工具链、partial thinking/reply 和 `[interrupted]` 标记。
- runtime 关机顺序覆盖 subagent、loop、scheduler、bus、channels、shell、plugins、MCP、memory worker、event bus 和 session index。

### 3.2 Reasoner 与 Provider

- 支持普通 chat completion 和 SSE，累积正文、`reasoning_content`、分片 tool call id/name/arguments。
- DeepSeek thinking history 会将每组工具调用时的 reasoning 原样回传。
- tool schema 在执行前检查 required、类型和 enum；错误作为 tool result 返回模型。
- deferred tool 默认不暴露，模型需经 `tool_search` 解锁；每个 session 保存 5 项 LRU。
- 未注册工具、未解锁工具和 turn metadata 禁用工具都不会执行。
- 连续重复相同 tool signature 达阈值后停止执行，转入阶段总结。
- 达最大 iteration 后额外请求一次不带工具的总结，避免只返回机械错误。
- 处理模型超时、空响应、429/5xx、context length 和 content safety。
- context retry 先缩减动态上下文与历史，再返回明确降级文本。

### 3.3 Session 与历史

- session 文件名为可读前缀加 SHA-256 短摘要，避免 `a:b` 与 `a/b` 清洗后碰撞。
- 写入采用同目录临时文件和 `os.replace`，异常时不破坏旧 session。
- 兼容旧安全文件名并按真实 key 校验迁移。
- history 从 user boundary 开始，不产生孤立 assistant/tool message。
- 每个 tool call group 重建 assistant tool_calls、reasoning 和逐 call tool result。
- tool result 回放有长度上限，防止历史无限膨胀。
- SQLite FTS5 trigram 仅作索引，JSON session 仍是事实源；索引可重建。
- session 删除触发 memory source undo，避免已删除对话继续污染长期记忆。

### 3.4 Memory

- `MEMORY.md` 由托管 block 与人工区组成；forget 会同步重写托管 block，而不删除人工内容。
- `items.json` 记录 id、type、source_ref、status、reinforcement、时间和可选 embedding。
- exact duplicate 强化旧记录；同 source 重放保持幂等。
- 中文 bigram、英文 token 和 substring 共同参与词法召回。
- 配置 embedding 后以语义 0.75 + 词法 0.25 混合评分；服务失败自动回退词法。
- recall 支持 memory type、since、until 过滤。
- 每轮同步更新 recent/history；达到窗口后异步调用 LLM 生成结构化 memories/history。
- 回复先返回；同 session 下一轮会等待上一轮 consolidation，超时后取消，避免读写竞态。
- consolidation、memorize 工具和显式记忆规则之间通过 source/idempotency 避免重复。

### 3.5 Plugin、Hook 与 MCP

- 支持 `.aka-plugin/plugin.json`，安全解析 lifecycle、skills 和 MCP 路径，拒绝目录穿越。
- 支持 `plugin.py` 类、entry、装饰器工具和 7 个 phase decorator。
- 插件有 `config.toml`、`config.local.toml`、可选 ConfigModel、原子 JSON KV 和独立 data dir。
- 插件 initialize 失败会撤销已注册工具、hooks、skills 和 MCP；坏插件不阻塞好插件。
- terminate 按加载逆序且幂等。
- pre hook 可改参数或 deny；pre hook 异常 fail-closed，post hook 异常隔离。
- MCP client 有 initialize handshake、并发 pending request、stderr drain、timeout、server error 和 disconnect 处理。
- MCP registry 能动态 add/remove/list、持久化 server 配置，并将远端工具注册为 deferred tool。
- `plugin_install` 只接受本地目录或 HTTPS Git URL，校验 manifest 后要求重启，不热执行刚下载代码。

### 3.6 工具与后台任务

- 文件工具限制工作区边界，原子写入，编辑时拒绝默认替换多个匹配，文本读取检测 NUL 二进制。
- shell 拒绝明显破坏性命令，使用独立进程组；timeout、turn cancellation 和 runtime shutdown 都会清理进程树。
- 长 shell 可显式后台运行，也可在前台阈值后自动转后台；`task_output` 支持 block/offset，`task_stop` 停止并清理日志。
- `web_fetch` 拒绝私网、loopback、link-local、reserved、multicast，且每次 redirect 重新校验；限制响应类型和 5 MB。
- vision 校验 PNG/JPEG/GIF/WebP magic bytes、单次总大小和路径边界。
- subagent 有独立 session、iteration 上限和 profile 权限；禁止递归 spawn、发消息、改 MCP/插件、创建 schedule。
- background subagent 上限为 3，提供 list/cancel，完成、失败、取消均回注原 session。
- schedule 只由用户 turn 显式创建，持久化 fire time/interval/status，不包含自主判断。

### 3.7 Channels

Web：

- 每个请求生成 correlation id，同 session 并发 HTTP 请求不会串回复。
- `/stop`/`/interrupt` 可取消 turn。
- 校验 body、session id 和附件路径。
- 提供 session/memory list、patch、delete 管理 API。
- `/events` 长轮询接收 schedule、subagent、message_push 等非请求绑定消息。

Telegram：

- long polling offset、allow list、文本/图片/文档入站、20 MB 限制和 reply context。
- 4096 字符友好分片、429 `retry_after`、出站文件。
- 监听 stream lifecycle，先 send 占位消息，再 edit live content，最终 edit 定稿。

QQ/OneBot：

- HTTP webhook token 鉴权、自消息忽略、消息去重。
- 私聊 allow list、群 allow list、逐群 require_at/allow_from。
- group session 中保留发送者身份，解析 structured/CQ 图片并下载。
- 出站文本、图片、文件和 OneBot retcode/status 校验。

## 4. 保留的实现差异

这些不是“主链路缺失”，但仍是与 Reference 不同的专用实现：

1. **高级记忆算法**：Reference 的 `default_memory`/`memory2`/Akasha 还有 LLM query rewrite、HyDE、sufficiency checker、procedure rule conflict、profile extractor、热度排序和图关系存储。Kirakira 当前提供稳定的 Markdown + typed records + FTS + optional embeddings，尚未移植这些实验性算法。
2. **A2A Peer Agent**：Reference 可按配置冷启动外部 A2A 服务并轮询远端任务。Kirakira 已有本地 subagent 和 MCP，但没有独立 A2A process manager/poller。
3. **前端 Dashboard**：Reference 带 React Dashboard、插件面板和更多诊断视图。Kirakira Web 侧提供 chat、session/memory 管理 API 和轻量页面，没有复制其前端工程。
4. **Transport**：Reference Web 主要使用 FastAPI/WebSocket，Telegram/QQ 使用更重的 SDK；Kirakira 使用标准库 HTTP、Telegram Bot API 和 OneBot HTTP，功能路径相同但 UI/transport 不逐行一致。
5. **Phase slot DAG**：Reference phase module 可声明 slot import/export 和依赖顺序。Kirakira phase module 为显式顺序链，context 可变并可 abort，扩展能力足够，但没有通用 slot dependency resolver。
6. **Plugin jobs**：Reference 插件可声明 interval/event background job。interval job 会形成无人消息时的后台执行，和本项目排除的主动自治边界重叠；Kirakira 保留 event/lifecycle handler 和用户 schedule，没有复制 interval plugin runner。
7. **安装向导和 TUI**：Reference 有 Click setup wizard、独立 IPC TUI。Kirakira 使用 `config.example.toml`、环境变量和本进程 REPL，部署步骤更直接但交互体验较轻。

## 5. 明确排除的 Reference 代码

- `proactive_v2/**`
- `agent/core/proactive_*`、`drift_turn.py`
- `bootstrap/proactive.py`
- proactive source、feedback、presence、energy、judge、sensor、anyaction、quota
- drift skills 自动选择和空闲执行
- 仅服务于 proactive/drift 的 memory optimizer、prompt 和脚本

## 6. 审计证据

- conda Python：`/home/xiang/.conda/envs/xingshu-vllm/bin/python`，Python 3.12。
- `compileall`：通过。
- `unittest discover -s tests -v`：83 项通过。
- `git diff --check`：通过。
- DeepSeek 普通响应：`deepseek-v4-flash` 返回 `ONLINE_OK`。
- DeepSeek streaming tool loop：41 个 stream delta，`write_file`、`read_file` 均 success，session 保存 2 组 tool chain。
- DeepSeek consolidation：`last_consolidated=6`，生成 2 条可检索记录并写入 HISTORY。

密钥仅注入单次测试进程环境，没有写入 tracked 文件。Reference 目录由 `.gitignore` 排除。
