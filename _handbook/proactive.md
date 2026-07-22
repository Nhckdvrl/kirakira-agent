# 主动推送（Proactive）

## 先理解它是什么

被动链路是"用户问、agent 答"。主动链路是"**agent 自己找你**"：没有用户消息，
由一个后台循环按电量模型自适应轮询数据源，判断此刻要不要主动发一条消息、发什么。

这是 Kirakira 区别于普通 chatbot 的第一条差异化链路（第二条是 [Drift](./drift.md)）。
参考 akashic 的 `proactive_v2` + `plugins/wake_proactive`；本项目是 MVP 复刻，
保留差异化本质，刻意不搬 phase-graph kernel / snapshot 热重载等 Tier-3 机制。

```text
ProactiveLoop.run()（后台 task，与 AgentLoop 并列，见 CoreRuntime.start_background）
  └─ while running:
     ├─ 电量模型算出本轮 tick 间隔 → 等待
     └─ _tick():
        ├─ Gate     目标就绪？被动链路空闲？（passive_busy_fn=AgentLoop.is_busy，避让在跑的 turn）
        ├─ Fetch    SourceRegistry.fetch_all() 并发拉所有源
        ├─ Ingest   三通道去重入库（proactive.db）
        ├─ Decide   alert 直推 → content 兴趣判断 → 都没推则 ↓
        │            └─ Drift（见 drift.md）
        └─ Deliver  bus.publish_outbound(metadata.proactive=true)
```

代码在 `kirakira_agent/proactive/`。

## 电量模型：为什么轮询频率会自己变

固定间隔轮询要么太吵、要么太迟钝。主动链路用一个**多时间尺度指数衰减**的"电量"
模型（`energy.py`，从参考照搬的纯数学）来自适应调频：

```text
E(t) = α·exp(-t/τ₁) + β·exp(-t/τ₂) + γ·exp(-t/τ₃)
       τ₁=30min 对话余温   τ₂=240min 当天语境   τ₃=2880min 关系连续性(48h)
```

- `t` = 距最后一条用户消息的时间。刚聊完 → E 高；久没动静 → E 衰减到低。
- 两条贡献函数合成 `base_score`：
  - `D_energy = 1 - E`（互动饥渴度：越久没聊越高）
  - `D_recent = log(1+k)/log(1+scale)`（语境丰富度：近期消息越多越高）
  - `base_score = 1 - (1-D_energy)(1-D_recent)`（软或：任一维度高都抬分）
- `base_score` 越高 → `next_tick_from_score` 给的间隔越短 → 轮询越频繁 → 越快触发。

结果：刚聊完不烦你（长间隔 `tick_interval_s0`），久没动静就加速（短间隔 `tick_interval_s1`），
`tick_jitter` 加随机抖动避免整点齐发。`last_user_at` 与近期消息数从目标 session 推导。

## 三个通道

一次 tick 从所有源拉到的事件按 `kind` 分三路，语义各不相同：

| 通道 | 用途 | 是否触发推送 | ACK |
| --- | --- | ---: | ---: |
| `alert` | 健康告警、日程提醒、异常 | 是，直接透传 | 需要 |
| `content` | RSS、新闻、社区内容 | 经 LLM 兴趣判断 | 需要 |
| `context` | 睡眠、在线、环境状态 | 否，只辅助判断 | 不需要 |

判断顺序（`loop.py:_tick`）：**alert 按严重度优先直推**（还有 alert 则尽快再轮询排空）→
有新 content 且不在冷却期时，候选**按新近度排序取前 N** 再做**兴趣判断** → 都没推 → 交给
**Drift**。`context` 从不单独触发，只作为本轮判断的背景注入 prompt。排序函数在
`contracts.py`（`rank_alerts` 按 severity、`rank_content` 按时间），对齐 reference 的 `rank_events`。

## 数据源：可插拔协议

主动链只理解 `source`、`channel` 和事件，不硬编码数据从哪来。任何实现
`ProactiveSource` 协议（`sources.py`）的对象都能注册：

```python
class ProactiveSource(Protocol):
    id: str
    channels: Sequence[str]
    async def fetch(self) -> list[dict]:  ...   # 返回带 kind/event_id 的事件
    async def ack(self, event_ids) -> None: ...  # 确认已投递的原始事件
```

MVP 内置 `FileInboxSource`：读 `<workspace>/proactive/inbox/<id>.jsonl`，每行一个事件；
ACK 后把 event_id 写入 `<id>.acked`，下次自动过滤。这样零依赖就能演示完整 fetch/ack 闭环。

**拓展位**：接入 MCP 数据源时，实现同一个协议（`fetch` 调 MCP fetch_tool、`ack` 调 ack_tool）
即可无缝替换，链路其余部分不感知。参考 akashic 正是用 MCP 插件声明 `ProactiveSourceSpec`。

## 事件生命周期与去重

`proactive.db`（`state.py`）负责让"重复 tick 不重复投递"：

```text
fetch → ingest（INSERT OR IGNORE，item_id=<source>:<event_id> 稳定去重）
      → unread（未读队列）
      → LLM 判断 send/skip
      → 投递成功 → consume（标记已消费）+ ack（回源确认）
```

- `item_id` 是稳定身份 `<source_id>:<event_id>`；同一事件多轮出现只入库一次。
- 推送后写 `push_state.last_push_at`，`delivery_cooldown_hours` 内抑制 content 刷屏（alert 不受限）。
- 只 ACK/consume 被 LLM 真正引用（`cited_ids`）的事件，没引用的留在未读队列等下轮。
- `expire_old` 每轮淘汰 `first_seen` 超过 `content_max_age_days`（默认 14 天）的未读 content，
  防止从不被引用的候选无界堆积（对齐 reference `_content_expired` 的绝对陈旧淘汰）。

## LLM 判断

`judge.py` 让模型在"要不要发、发什么"上做决策，输入 = 长期记忆 + 近期对话 +
**最近已推送的主动消息（避免重复）** + `PROACTIVE_CONTEXT.md` 规则 + 当前 context + 候选事件。

- **alert**：不问该不该发，只让模型把它自然化成一句符合语气的话。
- **content**：先做兴趣判断，宁缺毋滥；返回 `{"decision": "send|skip", "message", "cited_ids"}`。
  声称 send 却没 message 或没引用 → 视为无效，安全起见 skip。

> 为什么不用 forced tool call？kirakira 的 `ModelClient.complete` 不支持 `tool_choice`，
> 所以判断器让模型直接产出严格 JSON 再解析——同样是"LLM 决策"，且不依赖具体 provider 的
> forced-tool 能力。这是与参考的一处有意实现差异。

## 健壮性：判断失败不能拖垮后台循环

主动链路是 best-effort 后台任务，与被动链路的 fail-loud 取向不同。判断层的模型/解析
异常被捕获并**大声记日志**后降级，而不是让整个 tick 崩掉：

- **alert**（高优先级）：模型失败 → 回退发送原文标题，绝不因一次异常吞掉告警。
- **content**（可选）：模型失败 → 默认 skip，宁缺毋滥不打扰用户。

（`loop.run()` 也对 `_tick` 整体兜底，单轮异常不影响后续轮次。）

## 可观测性：决策 trace 与 status

每轮 tick 的结果都写入 `proactive.db` 的 `decisions` 表（`record_decision`）：
`alert_pushed` / `content_pushed` / `content_skipped` / `drift` / `idle` / `gated`。

`ProactiveLoop.status()` 返回一份快照：当前电量、base_score、下次间隔估计、三通道未读数、
上次推送时间、是否在冷却、最近 10 条决策、已注册源、Drift 是否启用。

按需触发（不等电量定时器）：`python -m kirakira_agent --proactive` 跑一次完整 tick 并打印
这份 status，方便演示与调试。

## PROACTIVE_CONTEXT.md：规则面板

`<workspace>/PROACTIVE_CONTEXT.md` 是用户/主 agent 维护的**规则**，判断器每轮读取并遵守：
白名单、黑名单、优先级、过滤条件。这里只定义规则，不提供内容事实。首次运行自动创建模板。

## 配置与开关

见 `config.toml` 的 `[proactive]` 段。`enabled=false`（默认）时循环根本不启动；
`enabled=true` 但没填 `[proactive.target]` 会记警告并跳过。交付复用现有 MessageBus，
推送消息打 `metadata.proactive=true`，并落一条 `proactive` 标记的 assistant 消息到目标
session，供后续 tick 感知"近期已推内容"避免重复。

## 验证清单

```text
┌─ enabled=false → ProactiveLoop 不构造（cli._build_proactive 返回 None）
├─ energy：久沉默 base_score 高、间隔短；刚聊完间隔长
├─ 三通道：alert 直推、content 走判断、context 不单独触发
├─ 排序：alert 按 severity、content 按新近度
├─ 去重：同一事件重复 tick 不重复投递
├─ 冷却：距上次推送不足 cooldown 时抑制 content
├─ 龄期：超龄未读 content 被淘汰
├─ 门控：被动链路忙时记 gated 决策并跳过（不进 drift）
├─ 健壮：判断层模型异常时 alert 发原文、content 默认 skip，不崩 tick
├─ 可观测：status() 含电量/未读数/最近决策；--proactive 手动触发
└─ 三路都空 → 进入 Drift（drift_hook 被调用）
```

对应测试：`tests/test_proactive.py`（16 项）。
