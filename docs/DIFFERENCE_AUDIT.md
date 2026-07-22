# Kirakira Agent 与 Akashic Agent 差异性评估

> 基准：本地 `Reference/` 的 Akashic Agent commit `012e37c`（2026-07-21）。本文评估当前工作树，
> 不把文件同名、类型声明或代码行数当成功能等价的证据。被动链路的工程演进见
> [VERSION_EVOLUTION.md](./VERSION_EVOLUTION.md)，主动链路自身的结构见
> [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md)。

## 1. 结论先行

Kirakira 不是 Akashic 的等比例缩小版。它采取了三种不同策略：

- **被动链路：语义对齐、结构压平。** 工具循环、Session、记忆检索、上下文治理、多渠道、MCP、
  插件 snapshot 等核心行为已覆盖，但用单进程和更少的抽象层实现。
- **主动推送：保留产品语义，运行时只做到 MVP。** 电量调频、alert/content/context、LLM 判断、
  去重冷却和消息交付已经贯通；Akashic 的 phase kernel、snapshot lease、插件数据源、hazard、
  embedding 兴趣与 durable outbox 尚未移植；pending ACK 已按 Reference 接入。
- **Drift：保留“行为由 SKILL.md 定义”的内核。** Kirakira 能跑一次带连续性的后台 Agent run，
  但没有 Akashic 的 journal、self-observation、hazard drive 和完整 lifecycle。

因此最准确的项目定位是：

> Kirakira 是一个参考 Akashic 语义、以可读单进程实现重建的 Agent Runtime。被动链路已形成较完整
> 的工程基座；主动推送与 Drift 已证明端到端闭环，但跨崩溃可靠提交、插件化和恢复连续性仍处于 MVP。

不能笼统地说“已复刻 Akashic”，也不应说“只是少了前端”。差距主要不在模型会不会调用工具，
而在动态运行时、主动链路的提交协议、可恢复状态和运维控制面。

## 2. 评估口径

本文统一使用四种状态，避免把“有一个相似文件”写成“已覆盖”：

| 状态 | 含义 |
| --- | --- |
| **对齐** | 关键输入、状态变化、失败语义和输出均有对应实现与测试 |
| **轻实现** | 用户可见行为成立，但结构、扩展点或恢复合同明显更简单 |
| **替代实现** | 目标相同，因本项目约束选择了不同机制，不能声称逐行移植 |
| **未实现** | 运行时能力不存在；即使有协议、文档或预留接口也不算完成 |

评估按五个问题核对：谁触发、谁拥有状态、什么时候提交、失败后怎样恢复、是否有调用点和测试。
代码行数只说明维护面，不说明覆盖率。当前 `kirakira_agent/` 约 2.3 万行 Python；Reference 排除
tests/eval 后约 10.6 万行。数倍的维护面差异本身正说明两者不是同一运行时厚度。

## 3. 架构层面的根本差异

| 维度 | Akashic Agent | Kirakira Agent | 判断 |
| --- | --- | --- | --- |
| 部署形态 | 多层 bootstrap/infra/control，带 app server、前端与监督能力 | 单进程 CLI/TUI + 内置 Channel host | Kirakira 更易读易跑，运维能力更弱 |
| 执行编排 | Turn/proactive lifecycle + slot DAG + side-effect abstraction | 显式顺序 pipeline，少量 phase hooks | 轻实现；固定流程清楚，第三方编排能力弱 |
| 动态代际 | per-plugin generation、snapshot、lease、quiesce/rollback | 全局单一 snapshot，主要保护被动 turn | 被动基本对齐；主动未绑定 lease |
| Provider | 多后端抽象与更完整控制能力 | OpenAI-compatible 一类接口 | 范围收窄；forced tool choice 等能力缺失 |
| 状态与持久化 | 统一 persistence 语义、更多 SQLite/恢复合同 | JSON/SQLite 分散在各模块 | 能运行，但一致性与恢复边界较弱 |
| 扩展生态 | 插件市场、plugin job、proactive source、lifecycle factory | workspace 插件 + hook/MCP；主动 source 为进程内协议 | 被动可扩展，主动扩展仍是预留位 |
| 控制面 | app server、control protocol、Dashboard、peer agent | 本进程命令与状态输出 | 有意未实现 |

Kirakira 的“轻”有真实收益：主链路可在较少文件内追踪，开发和演示成本低，也避免过早复制未被
使用的抽象。但当需求进入热插拔、跨进程、可靠投递和故障恢复时，这些差异会从“简化”变成必须补的能力。

## 4. 被动链路对照

### 4.1 已对齐或按比例实现

| Akashic | Kirakira | 状态 | 说明 |
| --- | --- | --- | --- |
| `bus/*`、被动 lane | `bus.py`、`event_bus.py` | 对齐 | 同 chat 保序、跨 session 并发、intercept/fanout |
| `agent/looping/*` | `runtime.AgentLoop` | 轻实现 | 消费、session 串行、中断成立，类型与层次更少 |
| `agent/core/passive_turn.py`、`turns/*` | `PassiveTurnPipeline` + `DefaultReasoner` | 轻实现 | streaming tool loop、retry、持久化成立；TurnResult/side effect 被内联 |
| lifecycle phases | `lifecycle.py` + EventBus | 轻实现 | 7 个 phase context；无 slot import/export DAG |
| prompting/context policy | `prompting/`、`context_builder.py`、`context_policy.py` | 对齐 | PromptBlock、预算预检、分级裁切、trace |
| retrieval pipeline | `retrieval.py` | 对齐 | lexical/vector 多路召回、RRF、热度、注入预算 |
| session manager/store | `session.py` | 轻实现 | JSON canonical + SQLite FTS；存储抽象更薄 |
| tool registry/hooks | `tools/registry.py`、`tool_hooks.py` | 对齐 | schema、pre/post/error、timeout、deferred discovery |
| MCP declarations/generation | `mcp/` + `snapshot.py` | 轻实现 | 声明式发布、失败回滚、在途 turn 固定代际 |
| Channel host | `channels/` | 替代实现 | Web/Telegram/QQ 行为覆盖，transport 更轻 |
| subagent/background | `subagent.py` | 轻实现 | inline/background 与并发上限；无 peer-agent 进程体系 |

### 4.2 记忆系统不是“全量搬运”

Kirakira 已具备 typed memory、异步 consolidation、source 关联、lexical/vector 检索、RRF、热度和
注入预算。它还把语义去重并入本来就要发生的 consolidation 调用，避免 Reference 的独立
`dedup_decider` 再增加一次模型往返。这是一个有意的成本优化。

仍未等价的部分包括 query rewrite、HyDE、sufficiency checker、独立 profile extractor、procedure
冲突判断、图关系存储与独立向量 store。仓库虽已有部分 `memory2/` 模块，默认被动调用链是否启用、
状态是否完整接入，必须逐项看 wiring，不能用目录存在来声称全量覆盖。

当前策略是合理的：先用评测集证明额外 LLM gate 能提升召回或写入质量，再承担延迟与费用。

### 4.3 已确认不是差距的两项

- Reference 的 `HistoryRoutePolicy` 没有实际实例化调用点，仅有导出；Kirakira 不复制死代码。
- Reference 的 delegation policy 当前实质是并发委派上限 3；Kirakira 的 background subagent 也限制为 3。
  Kirakira 缺的是结构化 reason/confidence 元数据，而不是并发门控行为。

## 5. 主动推送对照

### 5.1 能力映射

| Akashic | Kirakira | 状态 | 真实差异 |
| --- | --- | --- | --- |
| energy/scheduler | `proactive/energy.py` + `_next_interval()` | 替代实现 | 衰减与双档间隔沿用 Reference；Kirakira 每轮把 `D_energy` 与 `D_recent` 软或，Reference 还可接 lifecycle/hazard 产出的 base score |
| 三通道 contracts | `proactive/contracts.py` | 轻实现 | alert/content/context 语义保留，字段更少 |
| MCP proactive sources | `ProactiveSource` + `FileInboxSource` | 替代实现 | fetch/ack 接口成立，但没有 MCP/plugin 自动装配与 generation |
| source gateway/reservoir | `SourceRegistry` + `ProactiveStateStore` | 轻实现 | 并发 fetch、稳定 id、未读/消费成立；恢复合同更弱 |
| wake hazard/ranking | severity/newness 排序 | 轻实现 | 无累计 hazard、兴趣 embedding、turn prototype 校准 |
| forced tool decision | `ProactiveJudge` 严格 JSON | 替代实现 | provider 通用，但结构保证更弱 |
| `ProactiveKernel` + lifecycle | `ProactiveLoop._tick()` | 轻实现 | 显式顺序链，无 slot DAG、factory、start/stop rollback |
| proactive snapshot lease | 无 | 未实现 | tick 期间 source/tool/skill 可能不具备同代际保证 |
| delivery + pending ACK | Channel receipt + delivery id + pending ACK | 轻实现 | 对齐 orchestrator 的成功后提交与 wake state 的 ACK 队列；无 durable outbox |
| trace/diagnostics | `decisions` 表 + `status()` | 轻实现 | 能回看动作，缺完整 strategy/lifecycle trace |

### 5.2 产品语义已经成立的部分

以下不是“接口占位”，而是已有端到端调用点和测试的行为：

- 后台循环按 Session 活跃状态自适应选择下一次检查间隔。
- 所有 source 并发拉取，单源失败不阻断其他源。
- alert 优先于 content；content 由模型判断是否值得打扰；context 不独立触发。
- 稳定 item id 防止重复入库，content 有冷却和超龄淘汰。
- 模型失败时 alert 回退原文、content 默认 skip，后台循环继续存活。
- 主动消息走原 Channel，并写回 Session 供后续判断防重复。
- 被动 turn 忙时主动 tick 让路。

### 5.3 MVP 仍需明确承认的边界

1. **调度不是发送概率。** energy/base score 只选轮询间隔；是否发送由事件、冷却和 LLM 决定。
2. **单进程发送闭环已打通，但还不是跨崩溃 exactly-once。** 主动链路会等待 Channel callback 成功，
   失败不写 Session/不消费事件；仍缺 durable outbox，进程在渠道成功与本地提交之间崩溃时可能重复发送。
3. **文件源只是契约样例。** 没有生产级 RSS/日历/MCP source 的自动发现、凭据、健康检查和限流。
4. **主动链路没有代际租约。** 被动 turn 的 snapshot 经验尚未延伸到一次完整 proactive tick。
5. **单目标配置。** 当前一套 `[proactive.target]` 对应一个 channel/chat，不是多用户调度器。

这五项比“有没有 phase graph”更影响生产可用性，优先级应更高。

## 6. Drift 对照

| Akashic | Kirakira | 状态 | 说明 |
| --- | --- | --- | --- |
| `plugins/drift_flow` skill 驱动 | `drift/skills.py` + `runner.py` | 轻实现 | SKILL.md 决定行为，每次是一轮 Agent run |
| run/cursor/continuum | `drift.db` | 轻实现 | run 记录、skill continuum、min interval 已有 |
| message/finish tools | `drift/tools.py` | 替代实现 | 线程内生成草稿，主 event loop 等 Channel；成功记 sent，失败记 silent |
| hazard drive | 直接在 no-push 后 `maybe_run` | 替代实现 | 简单确定性门控，无到期采样 |
| journal/self-observation | 无 | 未实现 | 无 question/reinforce/revise 与 recent journal |
| proactive lifecycle integration | 函数 hook | 轻实现 | 无 module factory、slot 和 snapshot binding |

一个重要措辞边界是：Kirakira Drift **复用 Agent 和默认工具集**，并不完整复用
`PassiveTurnPipeline`。它没有自动获得被动 turn 的全部 lifecycle、streaming、context retry、snapshot
lease 与提交语义。

## 7. Kirakira 自己的差异，而不只是“少了什么”

与 Reference 相比，Kirakira 有几项明确的自主取舍：

- **单进程、显式 pipeline 优先。** 主动 tick 的全部控制流能在一个文件内读完，适合学习、调试和
  快速验证；代价是第三方 phase 组合能力较弱。
- **Provider 中立的 JSON 主动决策。** 不依赖某一家模型的 forced tool choice，接 OpenAI-compatible
  服务更容易；代价是结构化保证下降，需要更强 schema 校验。
- **把语义去重并入 consolidation。** 少一次模型调用，降低常态延迟和费用；代价是 prompt 职责更重，
  需要独立评测防止抽取与去重互相干扰。
- **Drift 草稿后提交。** 同步 Agent 在线程里不直接碰主 MessageBus，避免跨事件循环；这也自然形成
  “先生成副作用意图、再由 owner 提交”的边界。
- **文件源作为可执行示例。** 无外部依赖即可演示主动链路，但必须清楚标注它不是生产 source 方案。

这些差异只有在边界被写清时才是设计取舍；如果把可靠性缺口包装成“更轻”，就会掩盖风险。

## 8. 有意未纳入当前范围

| 能力 | 当前处置 | 原因 |
| --- | --- | --- |
| Dashboard / frontend | 未实现 | 独立产品面，不决定 Agent 核心语义 |
| app server / control protocol | 未实现 | 当前以本进程 CLI/TUI 为入口 |
| supervised restart / rolling backup | 未实现 | 运维与恢复层，尚未进入部署目标 |
| peer-agent 进程管理 | 未实现 | 当前 subagent 已满足本地委派范围 |
| 插件 marketplace | 未实现 | 现有 workspace 插件满足开发阶段 |
| 多 Provider backend | 未实现 | 当前只承诺 OpenAI-compatible |

“不在当前范围”不等于永远不需要。若目标从个人本地 Agent 变成多用户长期服务，control、backup、
租户隔离和可靠队列会立即变为核心要求。

## 9. 差距优先级

### P0：把单进程发送确认升级为跨崩溃可靠提交

1. 在已有 Channel delivery receipt 与 delivery id 上增加 durable outbox。
2. 为 proactive.db、drift.db 和 source cursor 定义备份/恢复与 crash replay 测试。

### P1：把 MVP 数据源变成可扩展运行时

1. 从插件/MCP 声明编译真实 `ProactiveSource`。
2. 一次 tick 绑定 snapshot lease，source generation 在 tick 内固定。
3. 增加 source timeout、health、rate limit 和 per-source diagnostics。

### P2：先可测，再增强智能

1. 建立 content precision、重复率、打扰率、alert 漏发率数据集。
2. 对比当前排序与 embedding prototype/hazard 的增益。
3. 给 JSON decision 做 schema retry，或扩展 Provider contract 支持 forced tool choice。
4. 建立 Drift 完成率、越权工具调用与 continuum 质量评测。

### P3：规模出现后再增加结构厚度

per-plugin generation、proactive phase DAG、多目标调度、app server、Dashboard 和 peer-agent 都应由实际
扩展或部署需求驱动，而不是为了目录看起来像 Reference。

## 10. 验证证据与限制

仓库中的主要证据包括：

- 被动链路：并发/保序、工具循环、Session、MCP generation、snapshot lease、context budget、memory
  retrieval、Channel 与 graceful shutdown 测试。
- 主动链路：`tests/test_proactive.py` 覆盖 energy、三通道、排序、去重、冷却、文件源 fetch/ack、
  alert/content 端到端 tick、Channel 失败保留、pending ACK 跨 tick flush、busy gate 与 status。
- Drift：`tests/test_drift.py` 覆盖 skill 发现、节流、continuum、Channel 确认与 sent/silent 修正。

现有测试证明“进程内闭环贯通到 Channel callback”，尚未证明：真实外部平台最终展示、渠道成功与
consume/ACK 的跨崩溃原子性、进程恢复、多目标公平调度、插件热换代中的主动 tick 一致性，以及
主动内容质量。这些正是上面 P0–P2 的依据。

## 11. 对外表述建议

可以说：

> 我参考 Akashic 的三链路语义，先重建了完整的被动 Agent Runtime，再实现主动推送与 Drift MVP。
> 主动链路用电量模型调节检查频率，把外部事件分成 alert/content/context，由 LLM 做内容判断；空闲时
> 运行用户可编辑的 Drift skill。当前会等待真实 Channel 发送成功后才提交事件；失败不写 Session，
> Drift 记为 `silent`；
> 下一阶段是 durable outbox、插件数据源与主动 snapshot。

不要说：

- “完整复刻 Akashic Agent”。
- “主动消息 exactly-once”。
- “Drift 完整复用被动 pipeline”。
- “已有 MCP proactive source”，除非完成自动装配和真实端到端验证。
- “代码少十倍但能力完全一致”。

这种表述既能说明差异化，也把 MVP 与生产级能力的边界讲清楚。
