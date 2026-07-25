# Kirakira Agent 主动链路架构

> 本文描述当前主动链路 MVP 的**实际架构、状态流转和边界**。它回答的是：没有用户消息时，
> runtime 如何决定何时检查外部世界、什么值得发给用户，以及没有内容可发时如何进入 Drift。
> 被动链路的演进过程见 [VERSION_EVOLUTION.md](./VERSION_EVOLUTION.md)，日常配置与操作见
> [_handbook/proactive.md](../_handbook/proactive.md) 和 [_handbook/drift.md](../_handbook/drift.md)。

> **结构更新(2026-07-25)**:主动 tick 已从扁平顺序链改为模块流水线,
> 顺序由各模块 `requires` 依赖图决定,Drift 是其中一个模块。调用链、失败语义与验收见
> [design/proactive-lifecycle.md](./design/proactive-lifecycle.md)。下文的分步描述仍成立,
> 只是每一步现在对应一个可被插件依赖的模块。

## 1. 先划清边界

被动链路和主动链路共享模型、记忆、Session、工具与 MessageBus，但有不同的触发器和提交语义：

| | 被动链路 | 主动链路 |
| --- | --- | --- |
| 触发器 | 用户入站消息 | 后台时钟或 `--proactive` |
| 核心问题 | 如何完成用户当前请求 | 此刻是否有值得主动打扰用户的事 |
| 主执行器 | `AgentLoop` → `PassiveTurnPipeline` | `ProactiveLoop` → `ProactiveJudge` |
| 工具循环 | 完整多轮 tool loop | 主动判断本身是一次结构化模型调用 |
| 无事可做 | 返回普通回复或结束 | 转入 Drift，或记录 idle |
| 出站 | `MessageBus` | 复用同一个 `MessageBus`，标记 `proactive=true` |

主动链路不是“给被动 Agent 加一个定时 prompt”。它在被动 turn 之外维护自己的调度、事件池、
去重、冷却和决策记录；只有交付层和上下文能力与被动基座复用。

当前实现还把两种主动行为分开：

- **Proactive push**：外部事件驱动。行为规则固定在代码中的判断 prompt，输入来自数据源。
- **Drift**：空闲驱动。行为来自用户可编辑的 `drift/skills/*/SKILL.md`，复用 Agent 去做后台任务。

## 2. 总体架构

```text
CoreRuntime
├─ AgentLoop                         用户消息驱动的被动链路
│  └─ PassiveTurnPipeline
│
└─ ProactiveLoop.run()               独立后台 task
   ├─ Scheduler                      energy + recent activity → 下次 tick 间隔
   └─ Tick
      ├─ Gate                        目标有效？该 session 的被动 turn 是否空闲？
      ├─ SourceRegistry              并发 fetch 所有 ProactiveSource
      ├─ Normalize / Ingest          alert、content 入库；context 仅保留本轮
      ├─ Decision
      │  ├─ alert                    取最高优先级事件，模型负责自然化表达
      │  ├─ content                  LLM 判断 send / skip，并返回 cited_ids
      │  └─ no push                  调用 DriftRunner.maybe_run()
      ├─ Delivery                    MessageBus.publish_outbound_and_wait()
      └─ State                       consume、pending source ACK、cooldown、decision trace

DriftRunner
├─ 发现并选择 SKILL.md
├─ 组装 memory + recent context + continuum briefing
├─ 在线程中运行一次同步 Agent.run()
├─ message_push 只生成草稿
└─ 回到主事件循环提交消息并写 drift.db
```

装配入口是 `cli._build_proactive()`。只有 `[proactive].enabled=true` 且目标 `channel/chat_id`
完整时才创建 `ProactiveLoop`；Drift 再由 `[proactive.drift].enabled` 独立控制。`CoreRuntime` 负责
后台 task 的启动与关闭。

### 2.1 Reference 对照点

本实现的提交顺序不是自行设计，直接对照以下 Reference 路径：

| Reference | Kirakira 对应 | 保留的语义 |
| --- | --- | --- |
| `Reference/agent/tools/message_push.py` | `MessageBus.publish_outbound_and_wait()` + Channel subscriber | 等待真实 sender 返回，失败不算已发送 |
| `Reference/agent/turns/outbound.py::PushToolOutboundPort` | Bus receipt 适配层 | 把 Channel 执行结果转成布尔提交结果 |
| `Reference/agent/turns/orchestrator.py` | `ProactiveLoop._deliver()` | 生成 `delivery_id`；发送成功后才写 Session，再跑 success side effect |
| `Reference/plugins/wake_proactive/runtime.py` | `_tick()` / `_flush_pending_acknowledgements()` | content 入库排队 ACK；alert 成功后 consume+queue ACK；启动与 tick 持续 flush |
| `Reference/plugins/wake_proactive/state.py` | `ProactiveStateStore` | `consume_and_queue_ack` 同事务，ACK 成功后才从 pending 表删除 |
| `Reference/plugins/drift_flow/runtime.py::record_commit_result` | `DriftRunner._commit()` | Channel 成功记 `sent`，否则记 `silent`，不自创草稿重发队列 |

Kirakira 没有 Reference 的全局 `MessagePushTool` 注册器，所以只有一个明确的框架适配：
用 waitable Bus envelope 把现有 Channel subscriber 的完成/异常变成 delivery receipt。这个适配不改变
Reference 的发送、Session 和副作用顺序。

## 3. 一次 tick 的确定性状态机

当前 `_tick()` 是显式顺序链，不是可插拔 phase graph：

```text
START
  │
  ├─ target 未配置 ─────────────────────────────────────→ END
  ├─ 同 session 被动链路忙 ─→ record(gated) ───────────→ END
  │
  └─ fetch_all()
       ├─ alert   → 持久化未读池
       ├─ content → 持久化未读池 + 标记本轮新增 id + 排队回源 ACK + 淘汰超龄项
       └─ context → 只形成当轮 prompt 背景，不持久化
            │
            ├─ 有未读 alert
            │    └─ 取一条 → 模型改写 → Channel 确认发送 → ACK + consume → END
            │
            ├─ 本轮有新增 content 且不在冷却期
            │    ├─ LLM send → Channel 确认发送 → consume cited_ids → END
            │    ├─ 发送失败 → 保留本地未读事件 → END
            │    └─ LLM skip ───────────────────────────────┐
            │                                               │
            └─ 无新增 content / 正在冷却 ──────────────────┤
                                                            ↓
                                                   Drift maybe_run
                                                            │
                                                   record(drift|idle) → END
```

这里有三个容易误读的语义：

1. **alert 是“必须尝试发送”，不是绕过模型原样直推。** 模型只负责把事件改写成自然消息；模型或
   JSON 解析失败时才回退到标题/正文。alert 不受 content 冷却限制。
2. **content 只在本轮出现新事件时触发判断。** 历史积压不会每 tick 重复调用模型；
   这与 Reference `should_evaluate_content = bool(state.contents and new_content_ids)` 一致。
   发送成功后只消费模型实际引用的 `cited_ids`。
3. **Drift 的入口是“本轮没有产生推送”，不是“三个通道严格为空”。** content 被 skip、处于冷却，
   或只有 context 时，都可能进入 Drift；被动链路忙导致的 gated tick 不会进入 Drift。

## 4. 调度层：电量决定检查频率，不决定发送

调度器先从目标 Session 推导最后一条用户消息时间与最近 24 小时的 user/assistant 消息数，再计算：

```text
E(t) = α·exp(-t/τ₁) + β·exp(-t/τ₂) + γ·exp(-t/τ₃)

D_energy = 1 - E(t)
D_recent = log(1 + recent_count) / log(1 + scale)
base_score = 1 - (1 - D_energy) × (1 - D_recent)
```

- 长期没有互动时，`D_energy` 高，检查会加快。
- 最近对话很丰富时，`D_recent` 高，检查也会加快，因为此时有更多可用于兴趣判断的语境。
- `base_score > 0.20` 使用 `tick_interval_s1`，否则使用 `tick_interval_s0`，再乘随机 jitter。

因此不能把它表述成“刚聊完一定降低频率”。当前模型的真实含义是：**长期沉默或近期语境丰富，
都会提高回看外部事件的频率**。是否打扰用户仍由 alert/content 语义、LLM 判断和 delivery cooldown
控制。电量只是调度信号，不是发送概率，也不是硬闸。

## 5. 数据源与三通道契约

核心链路只依赖 `ProactiveSource`：

```python
class ProactiveSource(Protocol):
    id: str
    channels: Sequence[str]

    async def fetch(self) -> list[dict]: ...
    async def ack(self, event_ids: Sequence[str]) -> None: ...
```

`SourceRegistry.fetch_all()` 用 `asyncio.gather` 并发读取所有源。单源失败会记录日志并返回空列表，
不会拖垮整轮；未知 `kind` 被丢弃。每个事件用 `<source_id>:<event_id>` 形成稳定 `item_id`。

| 通道 | 状态 | 排序 | 决策 | 消费 / ACK |
| --- | --- | --- | --- | --- |
| `alert` | 写入 `proactive.db` 未读池 | severity，再按时间 | 必发，模型只改写 | Channel 成功后消费并回源 ACK |
| `content` | 写入未读池，超龄淘汰 | 新近度 | LLM `send/skip` | 入库后 ACK 上游，发送成功后只消费被引用项 |
| `context` | 不持久化 | 无 | 仅进入当轮 prompt | 不消费、不 ACK |

MVP 内置 `FileInboxSource`，读取 `<workspace>/proactive/inbox/*.jsonl`，用同名 `.acked` 文件模拟
回源确认。它证明了 fetch/ack 契约，但不是生产级数据源总线；当前还没有把 workspace MCP 或插件
声明自动编译成 `ProactiveSource`。

## 6. 判断层与上下文

`ProactiveJudge` 的输入由六部分组成：长期记忆、近期被动对话、最近主动消息、
`PROACTIVE_CONTEXT.md`、当前 context、候选事件。

- `PROACTIVE_CONTEXT.md` 是规则面板，适合写白名单、黑名单、优先级和过滤条件，不应放新闻事实。
- 最近主动消息与被动对话分开读取，避免重复推送污染普通对话语境。
- content 判断必须返回合法 JSON、非空 message 和至少一个有效 `cited_id`；否则按 skip 处理。

Reference 使用 forced tool call 表达 send/skip。Kirakira 的 `ModelClient.complete()` 没有统一的
`tool_choice` 能力，因此 MVP 选择严格 JSON。这是 provider 能力边界导致的实现替换，不是功能等价
证明：JSON 解析与 schema 约束仍比真正的 forced tool contract 弱。

## 7. 状态、提交与失败边界

主动链路有三类持久状态：

- `proactive.db`：事件去重、消费状态、pending acknowledgements、最后推送时间和 decision trace。
- `<source>.acked`：内置文件源自己的回源确认状态。
- 目标 Session：已发送的主动 assistant 消息，供下一轮防重复。

当前提交顺序是：

```text
MessageBus.publish_outbound_and_wait() 入队
  → Bus 保持同 chat 顺序
  → 目标 Channel subscriber 完成 API / 本地 Channel 提交
  → delivery receipt 成功
  → 带 delivery_id 写 Session + mark_push
  → success side effect
       alert: consume + pending ACK 同事务落库，再 flush ACK
       content: consume cited_ids（content 在 ingest 时已排队 ACK）
```

主动消息不会在入队时提交：没有 Channel subscriber 或 Channel 发送失败，都会
得到 `OutboundDeliveryError`。alert 保持未读；content 保持本地未读，但仍遵循 Reference 的
“仅新 content 触发评估”语义。只有 Channel 回调成功后，才写冷却、Session 并消费相应事件。

pending ACK 与 Reference 一样在启动/每轮 tick 以及新 ACK 入队后 flush；只有 source 真正确认成功才删除。

这已经把 MVP 从“队列入队”贯通到“Channel API/本地 Channel 接收成功”。它仍不是跨进程 exactly-once：
如果进程在渠道成功与本地 SQLite 提交之间崩溃，仍可能重发。这属于 durable outbox/inbox
的下一层，不影响当前单进程端到端发送闭环已经成立。

失败策略按事件价值分级：

| 失败点 | 当前行为 | 设计取向 |
| --- | --- | --- |
| 单个 source fetch 失败 | 记录异常，其余源继续 | 部分可用 |
| alert 判断失败 | 回退标题/正文继续发 | 不吞高优先级事件 |
| content 判断失败 | skip | 宁缺毋滥 |
| Channel subscriber 不存在 | delivery failed，事件保留 | 禁止“无人接收也算发送” |
| Channel API 失败 | 事件保留，不提交 Session/消费状态 | 对齐 Reference 的单次 dispatch |
| source ACK 失败 | 保留 `pending_acknowledgements`，后续 tick 重试 | 只在成功后删除 |
| 整个 tick 异常 | 外层捕获，下一轮继续 | 后台循环存活优先 |
| 被动 turn 正忙 | 记录 gated，整轮跳过 | 被动请求优先 |

## 8. Drift 如何复用被动能力

Drift 不是 `PassiveTurnPipeline` 的后台版，而是复用较底层的同步 `Agent.run()` 与默认工具注册表：

1. `DriftRunner` 发现 `drift/skills/*/SKILL.md`，选择最久未运行的 skill。
2. 把 skill 正文作为 system prompt；把长期记忆、近期 context、上轮 continuum 和最近 run 组装成 briefing。
3. 在线程中执行一次有 `max_steps` 上限的 Agent run，并额外注册 `message_push`、`finish_drift`。
4. 线程内的 `message_push` 只保存草稿，避免跨事件循环访问 MessageBus。
5. run 返回主事件循环后等待 Channel 完成，再把结果与 continuum 写入 `drift/drift.db`。
6. Channel 成功则记 `sent` 并写 Session；失败则记 `silent`，不写 Session。这与 Reference
   `record_commit_result(sent)` 一致，不额外引入 Drift 草稿重发队列。

因此 Drift 复用了工具循环，但**没有自动继承被动 pipeline 的全部能力**，例如被动 turn 的 phase、
streaming、snapshot lease、完整 context budget 与 turn commit 语义。文档中应称为“复用同一 Agent/工具
能力”，而不是“完整复用被动链路”。

## 9. 当前 MVP 做到了什么

| 层 | 已实现 | 暂未实现 |
| --- | --- | --- |
| 调度 | 电量衰减、近期活跃度、双档间隔、jitter、手动 tick | hazard 到期采样、持久化 scheduler cursor |
| 来源 | 进程内 Source 协议、并发 fetch、文件源 | MCP/plugin source 自动装配、source generation |
| 决策 | 三通道语义、LLM content 判断、alert 降级 | 兴趣 embedding、turn prototype、forced tool contract |
| 状态 | 去重、未读池、pending ACK、Channel receipt、delivery id、冷却、超龄淘汰、decision trace | 事务化 outbox、备份恢复合同 |
| 并发 | 被动 busy gate、共享 MessageBus 保序 | proactive tick 的 snapshot lease、跨进程互斥 |
| Drift | skill 发现、节流、continuum、Channel receipt、sent/silent 修正 | hazard drive、journal/self-observation、完整 phase runtime |

这一定义了“MVP”的准确含义：端到端行为已经成立，架构边界也已显式留下；但它仍是单进程、单目标、
文件源优先、Channel-confirmed delivery 的实现，不等同于 Akashic 的可插件化和跨崩溃可恢复主动运行时。

## 10. 下一步按风险排序

1. **把发送确认升级为跨崩溃可靠提交**：当前已有 Channel receipt、delivery id 和
   pending ACK；下一层增加 durable outbox，消除“渠道成功后、SQLite 提交前”的崩溃窗口。
2. **接真实数据源**：把插件或 workspace MCP 声明编译为 `ProactiveSource`，并定义 source 的
   generation、超时、限流和健康状态。
3. **让 tick 绑定 runtime snapshot**：保证一次 tick 从 fetch 到 commit 看到同一代 source、tool、skill。
4. **建立主动决策评测集**：测 alert 漏发率、content precision、重复率和打扰率，再决定是否引入
   embedding 兴趣模型、query enrichment 或更复杂的 hazard。
5. **最后再抽 phase graph**：只有出现第三方 lifecycle module、多个主动策略或热插拔需求时，
   `ProactiveKernel`/slot DAG 的复杂度才有回报。

## 11. 代码与验证入口

| 关注点 | 入口 |
| --- | --- |
| 调度与 tick | `kirakira_agent/proactive/energy.py`、`loop.py` |
| 事件契约与排序 | `kirakira_agent/proactive/contracts.py` |
| 数据源 | `kirakira_agent/proactive/sources.py` |
| 去重、冷却、trace | `kirakira_agent/proactive/state.py` |
| LLM 判断 | `kirakira_agent/proactive/judge.py` |
| Drift | `kirakira_agent/drift/` |
| runtime 装配 | `kirakira_agent/cli.py::_build_proactive` |
| 回归测试 | `tests/test_proactive.py`、`tests/test_drift.py` |

手动运行 `python -m kirakira_agent --proactive` 会立即执行一个 tick 并等待目标 Channel 返回发送结果，
再打印 status。`delivery_failed`、未读数、冷却和最近决策都可用于核对完整发送链路。
