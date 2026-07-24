# Kirakira Agent

Kirakira Agent 是一个参考 `akashic-agent` 实现的多渠道 AI Agent Runtime。它不只是一个"你问它答"的 tool-calling demo，而是一个**会主动找你**的 AI 伙伴：统一接入 Web、Telegram、QQ/OneBot，既能被动回复，也能按电量模型自适应判断"此刻该不该发消息、发什么"，还能在空闲时自主执行后台任务。

> 想先看清“从最小 MVP 做到了哪里、三条链路怎么验收”，直接看
> [docs/MVP_TO_CURRENT.md](./docs/MVP_TO_CURRENT.md)。
> 启动、初始化向导及 Web/Telegram/两种 QQ 的完整合同见
> [docs/STARTUP_AND_CHANNELS.md](./docs/STARTUP_AND_CHANNELS.md)。

## 三条链路

和市面上多数 agent 只有"被动回复"一条链路不同，Kirakira 参考 akashic 实现了三条并列链路，后两条才是它区别于普通 chatbot 的地方：

```text
你的消息 ─→ [被动回复] ──→ agent loop ──→ 回复
              │
              ├── 记忆系统 ── 每轮注入长期记忆 + 回复后异步 consolidation
              │
              └── 插件系统 ── 拦截命令、注入 phase、阻断工具、挂载新工具...

[主动推送] ──→ 电量模型自适应轮询 ──→ 三路数据(alert/content/context) ──→ LLM 决策 ──→ 推送或跳过
              │
              └── [Drift] ──→ 本轮没有产生推送时,照 SKILL.md 自主干后台活儿
```

| 链路 | 触发方 | 行为定义在 | 差异点 |
| --- | --- | --- | --- |
| 被动回复 | 用户消息 | 代码（agent loop） | 与市面 agent 一致 |
| 主动推送 | 定时轮询 | 代码（system prompt） | 电量自适应节流 + 三通道语义 |
| Drift | 主动链路空转 | 你写的 `SKILL.md` | 行为可编辑、跨轮连续性 |

- **被动回复**：收到消息 → 记忆检索 → 工具循环 → 流式回复，每轮经过 7 个 phase 扩展点。
- **主动推送（Proactive）**：Agent 按电量模型和近期对话活跃度调整检查频率；每轮拉三路数据：`alert` 优先发送并由模型自然化表达、`content` 由 LLM 做兴趣判断、`context` 只辅助判断不单独触发。调度频率不等于发送概率，是否打扰仍受通道语义与冷却控制。
- **Drift**：主动链路本轮没有产生推送时，Agent 不空转，而是读你写在 `drift/skills/<name>/SKILL.md` 里的分步指南，一步步执行一个后台小任务（补用户画像、审计记忆等），带跨轮连续性，最后调 `finish_drift` 收尾。

详见 [_handbook/proactive.md](./_handbook/proactive.md) 与 [_handbook/drift.md](./_handbook/drift.md)。

## 被动回复架构

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

## 主动推送与 Drift 架构

```text
 ProactiveLoop.run()（后台 task，与 AgentLoop 并列）
             ↓
 电量模型决定本轮 tick 间隔（energy → base_score → interval）
             ↓
 Gate（目标就绪、被动链路空闲）
             ↓
 Fetch：SourceRegistry 并发拉取所有数据源
             ↓
 Ingest：三通道去重入库（proactive.db）
             ↓
 Decide：alert 优先发送 → content 兴趣判断 → 本轮没推则 ↓
             ↓
 Drift：DriftRunner 选一个 SKILL.md → 复用同步 Agent 与默认工具集跑一轮 → finish_drift
             ↓
 Deliver：bus.publish_outbound_and_wait(metadata.proactive=true) → 原 Channel 确认
```

## 已实现能力

- **主动推送链路（MVP）**：电量模型（多时间尺度指数衰减）自适应轮询节流；三通道 `alert`/`content`/`context` 语义；可插拔 `ProactiveSource` 协议 + 内置文件源；`proactive.db` 事件去重/pending ACK/冷却；LLM 兴趣判断决定推送或跳过；交付等待真实 Channel callback，成功后才带 `delivery_id` 写 Session 并消费事件。
- **Drift 空闲任务链路（MVP）**：主动链路本轮未产生推送时尝试进入；发现 `drift/skills/*/SKILL.md` 并每轮重新选择；一轮 Drift 复用同步 Agent 与默认工具集跑成一次 run，SKILL.md 当 system prompt + 注入 Briefing；`message_push` / `finish_drift` 收尾；`drift.db` 保存 run 记录、跨轮连续性与 `min_interval_hours` 门控。
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
- Reference Telegram 完整渠道：图片/文档与引用消息入站、UTF-16 分片、Markdown entities、429 重试、Conflict 处理、typing、工具/思考 live edit 和出站文件。
- QQ 图片入站、私聊/群聊策略、群发送者标识、OneBot 状态校验和出站媒体。
- Web 并发请求关联、主动消息长轮询、会话/记忆管理 API。
- Shell 进程组超时/取消、文件原子写入、编辑歧义保护、二进制检测。
- `web_fetch` 私网/重定向 SSRF 防护、响应类型和 5 MB 上限。
- `vision` 独立视觉模型工具，校验图片真实 magic bytes。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（推荐；项目已提交 `uv.lock`，首次运行自动创建隔离环境并安装锁定依赖）
- Runtime 依赖由 `uv.lock` 固定；Telegram 使用 Reference 相同的 `python-telegram-bot` 与 `telegramify-markdown`，全屏终端界面使用 Textual。
- Telegram、QQ、MCP 等能力通过 HTTP API 或 stdio 协议接入。

推荐使用现有 conda 环境：

```bash
conda activate xingshu-vllm
python -m unittest discover -s tests -v
```

## 首次配置（与 Reference 同入口）

推荐直接运行交互向导：

```bash
uv run python main.py setup
```

也可以直接运行 `uv run python main.py`；首次检测不到 `config.toml` 时会自动进入同一个向导，配置完成后继续启动完整服务。向导会生成权限为 `0600` 的 `config.toml` 与 `.env`，初始化 `~/.kirakira/workspace`，并配置 Web、Telegram、QQ/NapCat/OneBot、腾讯开放平台官方 QQBot、Proactive 和 Drift。

渠道初始化与 Reference 保持同一条完整链路：

- Telegram：验证 BotFather token，写入白名单；开启主动推送时监听一条新消息自动取得并验证 `chat_id`。
- 官方 QQBot：验证 AppID/AppSecret，通过 Gateway WebSocket Identify 监听第一条 C2C 消息取得 `user_openid`；主动目标写成 `qqbot / c2c:USER_OPENID`。
- QQ/NapCat/OneBot：配置 bot QQ、HTTP API、access token、私聊白名单和群白名单，初始化时校验 `get_status`；NapCat 事件上报到 `http://127.0.0.1:8766/qq/webhook`。
- Web：随主进程启动并只监听本机，默认地址 `http://127.0.0.1:6322`。

CI 或不需要问答时使用非交互初始化：

```bash
uv run python main.py init
```

仍可手动从示例开始：

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
port = 6322
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

Reference 风格的推荐入口会启动全部已配置 Channel、被动链路、Proactive 与 Drift：

```bash
uv run python main.py
```

显式启动未托管 runtime（调试别名）：

```bash
uv run python main.py gateway
```

默认命令与 Reference 一样先进入固定 supervisor，再由它启动 `gateway`。supervisor 对 workspace 加独占锁，等待带 boot ID/PID 的 readiness，转发 SIGINT/SIGTERM，并只接受当前 boot 私有管道上的合法重启提交；`gateway` 则绕开 supervisor，供调试器直接附着。旧的 `python -m kirakira_agent` 入口继续保留，用于本地 TUI/Plain 客户端。

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

Web 默认地址：<http://127.0.0.1:6322>

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

腾讯开放平台官方 QQBot 是独立渠道，不与 NapCat 配置混用：

```toml
[channels.qqbot]
enabled = true
app_id = "你的 AppID"
client_secret = "${QQBOT_CLIENT_SECRET}"
allow_from = ["你的 user_openid"]
channel_name = "qqbot"
```

官方 QQBot 使用 Gateway WebSocket 接收入站 C2C 消息，并使用 `/v2/users/{openid}/messages` 发送被动回复和主动消息；启动时会校验 access token，运行中负责 token 提前续期、心跳与断线重连。

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

## 开启主动推送与 Drift

主动链路默认关闭。填好目标渠道后开启：

```toml
[proactive]
enabled = true
# base_score 高（长期沉默或近期语境丰富）用短间隔，否则用长间隔；单位秒，tick_jitter 加抖动。
tick_interval_s1 = 2400
tick_interval_s0 = 4800
model = ""            # 留空复用 [llm.main].model

[proactive.target]
channel = "telegram"  # 推送目标渠道
chat_id = ""          # 你的 user id

[proactive.agent]
content_limit = 5
delivery_cooldown_hours = 1   # content 刷屏抑制（alert 不受限）

[proactive.drift]
enabled = true        # 建议先跑通 proactive 再开启
min_interval_hours = 3
max_steps = 20
```

启动后主动链路作为后台 task 运行，并会在被动 turn 进行时自动避让。`[proactive.target].channel`
会自动启用同名的内置 Web/Telegram/QQ Channel；目标找不到对应 Channel 时启动直接报错，不会等到
消息入队后静默丢失。想快速体验：往 `<workspace>/proactive/inbox/demo.jsonl` 投几条事件（格式见该目录
自动生成的 `README.md`），下一个 tick 就会完成判断、Channel 发送确认与事件提交。Drift 首次运行会
自动放两个示例 skill：`explore-curiosity`（会推送）与 `review-memory`（纯后台）。

不想等电量定时器？用一次性命令手动跑一个 tick 并打印状态，方便演示与调试：

```bash
python -m kirakira_agent --proactive
```

它会执行一次完整 tick（Gate→Fetch→Decide→Deliver→空则 Drift），然后打印当前电量、下次间隔估计、三通道未读数、上次推送时间和最近决策记录（`recent_decisions`），随后退出。

## 内置工具

工作区：`bash`、`task_output`、`task_stop`、`list_dir`、`read_file`、`write_file`、`edit_file`。

上下文：`load_skill`、`compact`、`tool_search`、`vision`。

记忆与历史：`memorize`、`recall_memory`、`forget_memory`、`search_messages`、`fetch_messages`。

网络与消息：`web_fetch`、`web_search`、`message_push`。

扩展：`plugin_install`、`plugin_list`、`plugin_doctor`、`spawn`、`spawn_manage`。

调度：`schedule`、`list_schedules`、`cancel_schedule`。

> 主动推送与 Drift 是**后台链路**而非工具：`ProactiveLoop` / `DriftRunner` 作为后台 task 运行。
> Drift run 内部额外挂载 `message_push`（覆盖为记草稿、由 runner 投递）与 `finish_drift` 收尾工具。

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
memory/MEMORY.md              人工维护的长期档案（与结构化 Memory2 独立）
memory/SELF.md                Agent 自我模型
memory/RECENT_CONTEXT.md      近期 turn 摘要
memory/HISTORY.md             幂等时间线记录
memory/PENDING.md             预留的待整理记忆文件
memory/coremem.db             唯一结构化长期记忆 owner（原 memory2.db）
memory/structured-owner.json  coremem/legacy 发布与回滚标记
memory/items.legacy.*.json    迁移前只读恢复点，不参与运行
uploads/                      Channel 附件
.kirakira/schedules.json      持久化定时消息
.kirakira/shell-tasks/        后台 shell 临时日志
.kirakira/subagent-runs/      后台子 Agent 结果
.kirakira/manifest.toml       插件启停清单（只记 enabled）
.kirakira/plugins/            安装的插件代码
.kirakira/plugin-data/        插件运行数据
mcp/servers/*.toml            workspace MCP 声明（热重载）
proactive.db                  主动链路事件去重 / ACK / 推送冷却
proactive/inbox/*.jsonl       内置文件数据源（每行一个事件）
PROACTIVE_CONTEXT.md          主动推送规则面板（主 agent 维护、判断器每轮读取）
drift/skills/<name>/SKILL.md  Drift 任务定义（你写，agent 每轮当 system prompt）
drift/drift.db                Drift run 记录、跨轮连续性、min_interval 门控
```

以上路径都相对 workspace 根解析。workspace 由 `--workspace` > `KIRAKIRA_WORKSPACE` >
`config.toml` 的 `[runtime].workspace` > 当前目录决定；不同 workspace 之间不共享任何状态。

## 测试

```bash
/home/xiang/.conda/envs/xingshu-vllm/bin/python -m compileall -q kirakira_agent tests
/home/xiang/.conda/envs/xingshu-vllm/bin/python -m unittest discover -s tests -v
```

当前离线回归为 `274 passed, 4 subtests passed`，覆盖工具、Session、并发、MCP、snapshot、
上下文、记忆引擎契约 + DI 服务、异步 model runtime、Reference Telegram/Supervisor 一致性、主动链路与 Drift。另已使用
`deepseek-v4-flash` 在线验证普通响应、SSE 工具循环、后台记忆 consolidation，以及 context
估算/实际 usage/下一轮 baseline 的完整观测链。API key 不进入仓库。

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
| 主动推送怎么判断、电量模型怎么调频、数据源怎么接 | [_handbook/proactive.md](./_handbook/proactive.md) |
| Drift 空闲任务怎么写、跨轮连续性怎么保存 | [_handbook/drift.md](./_handbook/drift.md) |

`docs/` 各司其职，不重复：

| 我想知道 | 看这里 |
| --- | --- |
| 从 MVP 到当前进度、三条链路如何启动验收 | [docs/MVP_TO_CURRENT.md](./docs/MVP_TO_CURRENT.md) |
| 简历文案与面试追问（含三链路差异化，**面试先看这篇**） | [docs/RESUME_INTERVIEW_GUIDE.md](./docs/RESUME_INTERVIEW_GUIDE.md) |
| 与 Reference 的能力级差异、主动 MVP 边界与差距优先级 | [docs/DIFFERENCE_AUDIT.md](./docs/DIFFERENCE_AUDIT.md) |
| 主动链路的总体架构、状态机、提交边界与演进顺序 | [docs/PROACTIVE_ARCHITECTURE.md](./docs/PROACTIVE_ARCHITECTURE.md) |
| 被动链路如何从 Function Calling MVP 一步步工程化 | [docs/VERSION_EVOLUTION.md](./docs/VERSION_EVOLUTION.md) |
