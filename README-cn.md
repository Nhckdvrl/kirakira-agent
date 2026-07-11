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
- `.aka-plugin/plugin.json`、插件 skills 软链接、插件 MCP 声明、插件配置与 KV 数据目录。
- `plugin_install`、`plugin_list`、`plugin_doctor`；安装后重启生效，不热执行新下载代码。
- stdio MCP JSON-RPC client、并发请求关联、工具注册、动态增删和持久化。
- deferred MCP/plugin tools 与 `tool_search select:<name>` 解锁。
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
- 不依赖第三方 Python 包即可运行核心 Runtime。
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

[agent]
max_tokens = 8192
max_iterations = 40

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

本地 CLI：

```bash
python -m kirakira_agent
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

扩展：`mcp_add`、`mcp_remove`、`mcp_list`、`plugin_install`、`plugin_list`、`plugin_doctor`、`spawn`、`spawn_manage`。

调度：`schedule`、`list_schedules`、`cancel_schedule`。

## 插件目录

运行时扫描：

```text
<workspace>/plugins/*
<workspace>/.kirakira/plugins/*
```

兼容结构：

```text
my-plugin/
  .aka-plugin/plugin.json
  plugin.py
  skills/
  mcp/servers.json
  config.toml
  config.local.toml
```

插件运行数据写入 `.kirakira/plugin-data/<plugin-name>/`；该目录不会提交到 Git。

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
.kirakira/plugins/            安装的插件代码
.kirakira/plugin-data/        插件运行数据
mcp_servers.json              动态 MCP server 配置
```

## 测试

```bash
/home/xiang/.conda/envs/xingshu-vllm/bin/python -m compileall -q kirakira_agent tests
/home/xiang/.conda/envs/xingshu-vllm/bin/python -m unittest discover -s tests -v
```

当前审计批次共 83 项测试通过，并已使用 `deepseek-v4-flash` 在线验证普通响应、SSE 工具循环和后台记忆 consolidation。API key 不进入仓库。

更详细的代码串联、数据流和与 Reference 的差异见：

- `docs/PROJECT_REPORT.md`
- `docs/DIFFERENCE_AUDIT.md`
- `docs/REPLICATION_PLAN.md`
- `docs/VERSION_EVOLUTION.md`：从 Function Calling MVP 到当前 Runtime 的工程演进
- `docs/RESUME_INTERVIEW_GUIDE.md`：简历文案、面试追问、Bug 闭环和后续升级边界
