# Kirakira Agent：从 MVP 到当前进度

> 这是项目的总入口。当前目标不是复制 Akashic 的全部厚度，而是先让
> **被动回复、主动推送、Drift 空闲任务**三条链路都能从触发走到实际结果，
> 再逐层增加可靠性、智能判断与插件化。

## 1. MVP 的统一验收标准

```text
用户发消息  → Agent 能执行工具并回复                 （被动链路）
外部来事件  → Agent 能判断并向指定 Channel 发送       （主动链路）
本轮没有推送 → Agent 能执行 SKILL.md 并正常收尾          （Drift 链路）
```

三条链路共享 `MessageBus`、Channel、Session、模型、记忆和工具，但触发方式不同。
“链路打通”必须看到真正的 Channel callback 或任务结果，不能只看到函数、队列或数据表存在。

## 2. 从最小 MVP 到现在

### 阶段 A：Function Calling MVP

最初只验证一件事：

```text
User → Model 选工具 → 执行工具 → 结果回填 Model → Final Text
```

这个阶段证明 Agent 能做事，但还没有持久会话、并发、真实 Channel、长期记忆和运行时扩展。

### 阶段 B：被动链路成为可运行 Runtime

```text
Web / Telegram / QQ / CLI
  → InboundMessage
  → MessageBus
  → AgentLoop（同 Session 串行，跨 Session 并行）
  → PassiveTurnPipeline
  → 记忆与上下文组装
  → Streaming Model + Tool Loop
  → Session Commit
  → OutboundMessage
  → 原 Channel 回复
```

在最小工具循环之上，逐步补齐了 ToolRegistry/Executor、Hook、MCP、Session、长期记忆、
PromptBlock、上下文预检、Streaming、Plugin、Subagent、Schedule 和多 Channel。当前被动链路
已经超过 MVP，是其他两条链路共享的工程基座。

### 阶段 C：主动推送 MVP

```text
后台 Tick
  → Gate（目标已配置、被动链路空闲）
  → SourceRegistry.fetch_all()
  → alert/content 去重入库，context 作为背景
  → alert 优先 / content 由 LLM 判断
  → MessageBus.publish_outbound_and_wait()
  → Web / Telegram / QQ sender
  → 成功后写 Session + delivery_id
  → consume / pending source ACK
```

当前内置文件 Source，用 `<workspace>/proactive/inbox/*.jsonl` 演示 fetch/ACK 闭环。
Channel 未注册或 sender 失败不会被当作已发送；只有 Channel 成功才写 Session 和消费状态。

### 阶段 D：Drift MVP

```text
主动链路本轮未产生推送
  → DriftRunner 按节流选择 drift/skills/*/SKILL.md
  → 组装记忆 + 近期上下文 + continuum Briefing
  → 运行一次同步 Agent + 默认工具集
  → message_push 可选生成草稿
  → finish_drift 收尾
  → Channel 成功记 sent，否则记 silent
  → 保存 run 与跨轮 continuum
```

Drift 不是另外一套执行引擎；它复用现有 Agent 和工具，只改变触发方式、system prompt 和收尾合同。

## 3. 当前进度

| 链路 | 当前状态 | 已经跑通 | 下一阶段，不阻塞 MVP |
| --- | --- | --- | --- |
| 被动 | 工程化基座 | 入站、记忆/上下文、多轮工具、Streaming、Session、原 Channel 出站 | 评测集、多租户、运维控制面 |
| 主动 | MVP 闭环 | Tick、文件 Source、三通道、LLM 判断、真实 Channel callback、Session、ACK | 真实 MCP/plugin Source、多目标、durable outbox |
| Drift | MVP 闭环 | SKILL 发现、Agent run、工具、finish、可选发送、run/continuum | hazard、self-observation journal、更完整 lifecycle |

当前离线回归结果：

```text
215 passed, 4 subtests passed
```

其中被动链路与 Channel 定向测试 35 项、主动链路与装配 27 项、Drift 6 项。
测试证明进程内链路能走到 Channel callback；真实 Telegram/QQ 平台最终展示仍取决于本地 token、
OneBot 和网络配置，不在离线测试中伪造“已验证”。

## 4. 启动与验证

### 4.1 准备配置

```bash
cp config.example.toml config.toml
```

至少填好 `[llm.main]` 的 model/base_url/api_key。先用 Web 验证被动链路：

```toml
[channels.chat]
enabled = true
host = "127.0.0.1"
port = 8765
channel_name = "web"
```

### 4.2 验证被动链路

```bash
.venv/bin/python -m kirakira_agent --serve
```

打开 `http://127.0.0.1:8765`，发送一条需要读取仓库文件的消息。能收到最终回复，且
`sessions/` 中出现对应 Session，就证明入站、工具循环、出站和持久化都已走通。

### 4.3 验证主动链路

推荐用 Telegram 做真实外部发送验收：

```toml
[channels.telegram]
enabled = true
token = "${TELEGRAM_BOT_TOKEN}"
allow_from = ["<your-user-id>"]
channel_name = "telegram"

[proactive]
enabled = true

[proactive.target]
channel = "telegram"
chat_id = "<your-user-id>"

[proactive.drift]
enabled = false
```

放入一条 alert：

```bash
mkdir -p proactive/inbox
printf '%s\n' '{"kind":"alert","event_id":"smoke-alert-1","title":"Kirakira 主动链路测试","content":"收到这条消息即表示主动发送成功","severity":"high"}' >> proactive/inbox/smoke.jsonl
```

立即执行一次 tick：

```bash
.venv/bin/python -m kirakira_agent --proactive
```

终端应打印 `alert_pushed`，Telegram 应收到消息，`proactive/inbox/smoke.acked` 应出现
`smoke-alert-1`，目标 Session 中应有 `proactive=true` 和 `delivery_id`。

### 4.4 验证 Drift

先确保 inbox 没有未读 alert/新 content，再开启：

```toml
[proactive.drift]
enabled = true
min_interval_hours = 0
max_steps = 20
```

再执行：

```bash
.venv/bin/python -m kirakira_agent --proactive
```

本轮没有推送候选时会进入 Drift。检查 `drift/drift.db` 的 run 记录；若选中的 skill 调用
`message_push`，还会经过同一个目标 Channel 发送。

### 4.5 离线回归

```bash
.venv/bin/python -m pytest tests -q
```

## 5. 现在不做什么

以下能力有价值，但不应在三条链路的基础闭环之前抢优先级：

- 主动 phase graph 与完整 lifecycle factory。
- 兴趣 embedding、hazard 概率模型和 self-observation journal。
- 多目标/多用户主动调度。
- durable outbox 与跨进程 exactly-once。
- Dashboard、多租户服务化与运维控制面。

下一阶段应从真实使用问题反推：先接一个真实 Source，记录漏发、误发、重复与打扰率，
再决定是否引入 Reference 更厚的 hazard、embedding 或可靠队列。

## 6. 文档导航

- [VERSION_EVOLUTION.md](./VERSION_EVOLUTION.md)：只讲被动链路从 Function Calling MVP 到 Runtime 的演进。
- [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md)：主动推送与 Drift 的数据流、提交边界和 Reference 对照。
- [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md)：当前工作树与 Akashic 的能力级差异。
- `../_handbook/`：每个子系统当前真实的运行合同。
