# Kirakira Agent

Kirakira Agent 是一个参考 `akashic-agent` 被动回复架构实现的多渠道 AI Agent Runtime。它不再只是一个最小 tool-calling demo，而是一套可持续扩展的运行时：统一接入 Web、Telegram、QQ/OneBot，经过消息总线、会话隔离、长期记忆、生命周期插件和多轮工具循环后回复用户。

本项目明确不包含 `proactive_v2`、drift、自主传感器、自主判断后主动触达等主动链路。用户明确创建的定时消息、当前 turn 触发的子代理和 `message_push` 属于被动请求产生的副作用，仍在实现范围内。

## 架构

```text
Web / Telegram / QQ / CLI
             ↓
       InboundMessage
             ↓
        MessageBus
             ↓
 AgentLoop（跨会话并行、同会话串行、支持中断）
             ↓
   PassiveTurnPipeline
             ↓
 BeforeTurn → BeforeReasoning → PromptRender
             ↓
 DefaultReasoner（streaming LLM tool loop）
             ↓
 ToolExecutor（校验、超时、插件 Hook、执行）
             ↓
 AfterReasoning → Session commit → AfterTurn
             ↓
       OutboundMessage
             ↓
      原 Channel 回复

回复完成后：后台 memory consolidation
下一轮开始前：等待同 session 上一次 consolidation 收口
```

## 已实现能力

- Web、Telegram、QQ/OneBot、CLI 被动消息入口。
- 按 session 串行、跨 session 并行的 AgentLoop；`/stop` 中断并保存续跑标记。
- OpenAI-compatible 普通响应和 SSE streaming；支持分片 tool call 参数重组。
- DeepSeek `reasoning_content` 在工具链和历史中的完整回放。
- JSON 会话原子持久化，安全文件名避免 session key 碰撞。
- SQLite FTS5 trigram 消息索引，以及 `search_messages` / `fetch_messages` 回源。
- Markdown 长期记忆、类型化 memory item、强化次数、遗忘一致性、时间/类型过滤。
- 可选 OpenAI-compatible embedding，启用后执行语义+词法混合检索；失败自动回退词法。
- 回复后的异步 LLM consolidation；显式“记住”与 `memorize` 调用具备幂等语义。
- Lifecycle EventBus、7 个 turn phase 扩展点、工具开始/完成和 streaming 事件。
- 插件工具、生命周期模块、`@tool`、`@on_tool_pre` 和 phase decorators。
- 插件用 `plugin.py` 程序化声明能力；`manifest.toml` 只管启停；skills 软链接、配置与 KV 数据目录。
- `plugin_install`、`plugin_list`、`plugin_doctor`；安装后重启生效，不热执行新下载代码。
- stdio MCP JSON-RPC client、并发请求关联、声明式热重载、整批候选语义。
- 运行时能力快照 + 每 turn 租约：热重载不会抽走在途 turn 的工具，旧 MCP 进程等租约排空才断开。
- deferred MCP/plugin tools 与 `tool_search select:<name>` 解锁。
- 按模型 `context_window` 派生 `memory_window` 与输出预留；预算覆盖 system、历史、工具 schema 与图片。
- Reference 风格的具名 PromptBlock、动态 Context Frame、静态块缓存和语义化降级重试；每轮 trace 会保存到 session。
- workspace 隔离：运行时状态按 `--workspace` / `KIRAKIRA_WORKSPACE` / config 解析。
- inline/background `spawn` 子代理，独立 session、三类权限 profile、并发上限、list/cancel 和完成回注。
- 后台 shell、`task_output`、`task_stop`，timeout、取消和 runtime 关机均清理进程组。
- 用户请求创建的持久化 `schedule` / `list_schedules` / `cancel_schedule`。
- Telegram 图片/文档入站、长消息分片、429 重试、live edit、出站文件。
- QQ 图片入站、私聊/群聊策略、群发送者标识、OneBot 状态校验和出站媒体。
- Web 并发请求关联、主动消息长轮询、会话/记忆管理 API。
- Shell 进程组超时/取消、文件原子写入、编辑歧义保护、二进制检测。
- `web_fetch` 私网/重定向 SSRF 防护、响应类型和 5 MB 上限。
- `vision` 独立视觉模型工具，校验图片真实 magic bytes。

## 环境要求

- Python 3.11+
- 核心 Runtime 只使用标准库；全屏终端界面使用 Textual，安装项目时会自动安装。
- Telegram、QQ、MCP 等能力通过 HTTP API 或 stdio 协议接入。

推荐使用现有 conda 环境：

```bash
conda activate xingshu-vllm
python -m unittest discover -s tests -v
```

## 配置

推荐从示例开始：

```bash
cp config.example.toml config.toml
```

DeepSeek 配置：

```toml
[llm.main]
model = "deepseek-v4-flash"
api_key = "${DEEPSEEK_API_KEY}"
base_url = "https://api.deepseek.com/v1"
enable_thinking = false
context_window = 128000  # 以模型供应商公布的容量为准

[agent]
max_tokens = 8192
max_iterations = 40

[agent.context]
effective_context_percent = 0.9
# memory_window 不写时按 context_window 派生；只有明确要覆盖策略时才填写。

[channels.chat]
enabled = true
host = "127.0.0.1"
port = 8765
```

环境变量优先于 `config.toml`。旧 `.env` 配置仍兼容：

```bash
OPENAI_COMPATIBLE_BASE_URL=https://api.deepseek.com
OPENAI_COMPATIBLE_API_KEY=your-key
MODEL_ID=deepseek-v4-flash
```

DeepSeek V4 默认关闭 thinking。如需开启：

```bash
OPENAI_COMPATIBLE_THINKING=enabled
```

## 启动

本地 CLI 默认在交互终端启动全屏 TUI，并实时展示模型增量、推理片段、工具状态和耗时：

```bash
python -m kirakira_agent
```

也可以显式选择界面：

```bash
python -m kirakira_agent --tui    # 强制全屏 TUI
python -m kirakira_agent --plain  # 流式纯文本，适合日志、管道和不支持全屏的终端
python -m kirakira_agent --session research  # 直接继续名为 research 的本地对话
```

不带 `--session` 启动时，每次都会进入一个全新的空白对话；发送第一条消息后自动保存到 workspace 的 `sessions/` 目录。TUI 中输入 `/sessions` 会打开历史选择器，使用 `↑` / `↓` 和 `Enter` 即可恢复；也可以输入 `/session <名称>` 或下次用相同的 `--session <名称>` 直接续接。`/clear` 和 `Ctrl+L` 只清空屏幕，不删除历史。

每轮开始时，TUI 会显示类似 `Context · full · 3.1k tokens` 的状态；Plain CLI 会显示
`context full · 3106/891808 tokens · 0 history`。如果超出模型输入预算，会看到
`trim_skills_catalog`、`trim_recent_context` 等具名重试，而不是静默截断。完整 attempt、section、
缓存命中、模型实际 usage 和下一轮 history baseline 会保存到 assistant 消息的 `context_trace`
与 session metadata 的 `context_budget`。具体合同见
[_handbook/context-management.md](./_handbook/context-management.md)。

TUI 快捷键：`Enter` 发送，`↑` / `↓` 浏览输入历史，`Ctrl+C` 中断当前 turn（空闲时退出），`Ctrl+L` 清空当前视图，`Ctrl+Q` 退出。tmux 只负责保活和重新连接，界面本身由项目内的 Textual 客户端实现。

在 tmux 中后台启动并重新进入：

```bash
tmux new-session -d -s kirakira-cli 'python -m kirakira_agent --tui'
tmux attach -t kirakira-cli
```

按配置启动所有 Channel：

```bash
python -m kirakira_agent --serve
```

临时强制启用指定 Channel：

```bash
python -m kirakira_agent --serve --web
python -m kirakira_agent --serve --telegram
python -m kirakira_agent --serve --qq
```

Web 默认地址：<http://127.0.0.1:8765>

QQ/NapCat/OneBot HTTP 上报地址：

```text
http://127.0.0.1:8766/qq/webhook
```

## Telegram 与 QQ

Telegram 可以写入 `config.toml`：

```toml
[channels.telegram]
token = "${TELEGRAM_BOT_TOKEN}"
allow_from = ["123456789", "username"]
```

QQ 支持私聊白名单和逐群策略：

```toml
[channels.qq]
bot_uin = "12345"
api_base_url = "http://127.0.0.1:3000"
allow_from = ["10001"]
require_at = true

[[channels.qq.groups]]
group_id = "777"
allow_from = ["10001", "10002"]
require_at = true
```

## 可选语义记忆

不配置 embedding 时使用中文 bigram + 英文 token 词法检索。配置后自动使用混合检索：

```toml
[memory.embedding]
model = "text-embedding-v3"
api_key = "${EMBEDDING_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
```

## 可选视觉模型

主模型不支持图片时，配置 `vision` 工具：

```bash
VISION_MODEL_ID=qwen-vl-plus
VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_API_KEY=your-key
```

## 内置工具

工作区：`bash`、`task_output`、`task_stop`、`list_dir`、`read_file`、`write_file`、`edit_file`。

上下文：`load_skill`、`compact`、`tool_search`、`vision`。

记忆与历史：`memorize`、`recall_memory`、`forget_memory`、`search_messages`、`fetch_messages`。

网络与消息：`web_fetch`、`web_search`、`message_push`。

扩展：`plugin_install`、`plugin_list`、`plugin_doctor`、`spawn`、`spawn_manage`。

调度：`schedule`、`list_schedules`、`cancel_schedule`。

> MCP 不再有 `mcp_add` / `mcp_remove` / `mcp_list`。server 由 `<workspace>/mcp/servers/*.toml`
> 声明并热重载，见 [_handbook/workspace-mcp.md](./_handbook/workspace-mcp.md)。

## 插件目录

运行时扫描：

```text
<workspace>/plugins/*
<workspace>/.kirakira/plugins/*
```

插件结构（能力由 `plugin.py` 用代码声明，没有描述符文件）：

```text
my-plugin/
  plugin.py                     必需：入口 + 能力声明
  skills/                       可选：由 skill_roots() 声明
  config.toml
  config.local.toml
```

插件运行数据写入 `.kirakira/plugin-data/<plugin-name>/`；该目录不会提交到 Git。
完整契约见 [_handbook/plugins.md](./_handbook/plugins.md)。

## 数据目录

```text
sessions/                     JSON session + SQLite FTS 索引
memory/MEMORY.md              人工内容 + Runtime 托管长期记忆块
memory/SELF.md                Agent 自我模型
memory/RECENT_CONTEXT.md      近期 turn 摘要
memory/HISTORY.md             幂等时间线记录
memory/PENDING.md             预留的待整理记忆文件
memory/items.json             类型化 memory item
uploads/                      Channel 附件
.kirakira/schedules.json      持久化定时消息
.kirakira/shell-tasks/        后台 shell 临时日志
.kirakira/subagent-runs/      后台子 Agent 结果
.kirakira/manifest.toml       插件启停清单（只记 enabled）
.kirakira/plugins/            安装的插件代码
.kirakira/plugin-data/        插件运行数据
mcp/servers/*.toml            workspace MCP 声明（热重载）
```

以上路径都相对 workspace 根解析。workspace 由 `--workspace` > `KIRAKIRA_WORKSPACE` >
`config.toml` 的 `[runtime].workspace` > 当前目录决定；不同 workspace 之间不共享任何状态。

## 测试

```bash
/home/xiang/.conda/envs/xingshu-vllm/bin/python -m compileall -q kirakira_agent tests
/home/xiang/.conda/envs/xingshu-vllm/bin/python -m unittest discover -s tests -v
```

当前自动化测试共 186 项：183 项通过、3 项按环境条件跳过。另已使用 `deepseek-v4-flash` 在线验证普通响应、SSE 工具循环、后台记忆 consolidation，以及 context 估算/实际 usage/下一轮 baseline 的完整观测链。API key 不进入仓库。

## 文档

`_handbook/` 是各子系统的**心智模型与契约**：描述现在是什么、规矩是什么、错了会怎样。
它跟代码同一个 commit 更新。

| 我想知道 | 看这里 |
| --- | --- |
| 怎么声明一个 MCP server、改坏了会怎样 | [_handbook/workspace-mcp.md](./_handbook/workspace-mcp.md) |
| 热重载为什么不会打断正在跑的 turn | [_handbook/snapshot-and-lease.md](./_handbook/snapshot-and-lease.md) |
| 怎么写插件、怎么声明能力 | [_handbook/plugins.md](./_handbook/plugins.md) |
| Prompt 怎么分块、超限如何降级、trace 在哪里 | [_handbook/context-management.md](./_handbook/context-management.md) |
| Session 与长期记忆为什么分开 | [_handbook/memory.md](./_handbook/memory.md) |
| TUI 是否基于 tmux、流式终态和历史 Session 怎么工作 | [_handbook/cli-and-sessions.md](./_handbook/cli-and-sessions.md) |

`docs/` 是**历史与方法论**：记录为什么变成今天这样。

| 我想知道 | 看这里 |
| --- | --- |
| 从 MVP 一步步长成现在这样的过程 | [docs/VERSION_EVOLUTION.md](./docs/VERSION_EVOLUTION.md) |
| 可迁移的架构判断（分层、声明式、代际、错误边界） | [docs/ARCHITECTURE_LESSONS.md](./docs/ARCHITECTURE_LESSONS.md) |
| Handbook 是什么、为什么有用、怎么写 | [docs/HANDBOOK_GUIDE.md](./docs/HANDBOOK_GUIDE.md) |
| 与 Reference 的逐项差异和有意未跟进项 | [docs/DIFFERENCE_AUDIT.md](./docs/DIFFERENCE_AUDIT.md) |
| 代码串联与数据流 | [docs/PROJECT_REPORT.md](./docs/PROJECT_REPORT.md) |
| 复刻范围与完成清单 | [docs/REPLICATION_PLAN.md](./docs/REPLICATION_PLAN.md) |
| 简历文案与面试追问 | [docs/RESUME_INTERVIEW_GUIDE.md](./docs/RESUME_INTERVIEW_GUIDE.md) |
