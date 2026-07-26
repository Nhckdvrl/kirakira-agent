# Kirakira Agent 与 Akashic Agent 差异性评估

> 基准：本地 `Reference/` 的 Akashic Agent commit `012e37c`（2026-07-21）。本文评估当前工作树，
> 不把文件同名、类型声明或代码行数当成功能等价的证据。本文的"已对齐/已跑通"以离线测试为准,
> 其中哪些另有真实模型与真实渠道的证据,见 [design/live-verification.md](./design/live-verification.md)。被动链路的工程演进见
> [VERSION_EVOLUTION.md](./VERSION_EVOLUTION.md)，主动链路自身的结构见
> [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md)。

## 1. 结论先行

Kirakira 不是 Akashic 的等比例缩小版。它采取了三种不同策略：

- **被动链路：语义对齐、结构压平。** 工具循环、Session、记忆检索、上下文治理、多渠道、MCP、
  插件 snapshot 等核心行为已覆盖，但用单进程和更少的抽象层实现。
- **主动推送：保留产品语义，运行时只做到 MVP。** 电量调频、alert/content/context、LLM 判断、
  去重冷却和消息交付已经贯通；插件数据源已可编译接线,跨崩溃投递去重已按 Reference 的
  deliveries 表实现;仍缺 phase kernel、snapshot lease、hazard 与 embedding 兴趣。
- **Drift：保留“行为由 SKILL.md 定义”的内核。** Kirakira 能跑一次带连续性的后台 Agent run，
  journal / self-observation / hazard 采样到期已补,Drift 也已成为主动流水线上的一个模块;仍缺 Reference 的 module factory 与 start/stop rollback。

因此最准确的项目定位是：

> Kirakira 是一个参考 Akashic 语义、以可读单进程实现重建的 Agent Runtime。被动链路已形成较完整
> 的工程基座；主动推送与 Drift 已证明端到端闭环，但跨崩溃可靠提交、插件化和恢复连续性仍处于 MVP。

不能笼统地说“已复刻 Akashic”，也不应说“只是少了前端”。差距主要不在模型会不会调用工具，
而在动态运行时、主动链路的提交协议、可恢复状态和运维控制面。

### 1.1 启动与初始化

当前已经对齐 Reference 的用户侧命令契约：仓库根目录 `main.py`、`uv run python main.py`、`setup`、`init`、`gateway`、`--config`、`--workspace`。首次缺少配置时会自动进入 setup；向导只生成 Kirakira runtime 真正支持的 LLM、Web、Telegram、OneBot、Proactive 与 Drift 配置。

默认入口外层现已按 Reference 接入固定 supervisor：workspace 独占、gateway child、boot readiness、信号转发和私有重启提交校验均已落地；`gateway` 保持未托管调试语义。尚未对齐的是 Reference 上层的 `agent_restart` 工具准入协调器，因此当前 runtime 不会主动请求 supervisor 换代。

Telegram 的五个 `infra/channels` 源文件已经与固定 Reference 逐字节一致，差异只存在于文件外的
namespace、MessageBus、SessionManager、message-push 和 interrupt binding。固定 Supervisor 源文件也
以同样方式保持一致，Kirakira gateway 只在外部识别其 boot 环境。渠道层也不再把两种 QQ 混为一谈：
`channels.qq` 对应 NapCat/OneBot；`channels.qqbot` 对应腾讯开放平台官方 QQBot。QQ 两条实现尚未完成
本轮要求的一比一移植，不能与 Telegram 一起标成已对齐。

## 2. 评估口径

本文统一使用四种状态，避免把“有一个相似文件”写成“已覆盖”：

| 状态 | 含义 |
| --- | --- |
| **对齐** | 关键输入、状态变化、失败语义和输出均有对应实现与测试 |
| **轻实现** | 用户可见行为成立，但结构、扩展点或恢复合同明显更简单 |
| **替代实现** | 目标相同，因本项目约束选择了不同机制，不能声称逐行移植 |
| **未实现** | 运行时能力不存在；即使有协议、文档或预留接口也不算完成 |

评估按五个问题核对：谁触发、谁拥有状态、什么时候提交、失败后怎样恢复、是否有调用点和测试。
代码行数只说明维护面，不说明覆盖率。当前 `kirakira_agent/` 约 3.4 万行 Python；Reference 排除 tests/eval 后约 10.5 万行。数倍的维护面差异本身正说明两者不是同一运行时厚度。

## 3. 架构层面的根本差异

| 维度 | Akashic Agent | Kirakira Agent | 判断 |
| --- | --- | --- | --- |
| 部署形态 | 多层 bootstrap/infra/control，带 app server、前端与监督能力 | setup/init + supervisor/gateway + 内置 Channel host；有 Memory2 M1 管理 API，无完整 app server/前端 | 进程启动已对齐，控制面仍更轻 |
| 执行编排 | Turn/proactive lifecycle + slot DAG + side-effect abstraction | `TurnResult` 副作用提交单点 + `phase.py` slot 拓扑排序 | 对齐；模块签名尚未迁到 frame |
| 动态代际 | per-plugin generation、snapshot、lease、quiesce/rollback | 全局 snapshot + per-plugin 代际与租约、热重载换代 | 对齐；主动 tick 仍未绑定 lease |
| Provider | 多后端抽象与更完整控制能力 | OpenAI-compatible 一类接口 | 范围收窄；forced tool choice 等能力缺失 |
| 状态与持久化 | 统一 persistence 语义、更多 SQLite/恢复合同 | JSON/SQLite 分散在各模块 | 能运行，但一致性与恢复边界较弱 |
| 扩展生态 | 插件市场、plugin job、proactive source、lifecycle factory | 声明式规格 + 作业/服务 host + 插件主动源编译 + 安装免重启 | 骨架对齐；缺包元数据与非 git 源 |
| 控制面 | app server、control protocol、Dashboard、peer agent | **control protocol 已对齐**(JSON-RPC/NDJSON/Unix socket)+ Memory 管理 API | 协议与 turn 编排对齐;app server、Dashboard、peer agent 未实现 |

Kirakira 的“轻”有真实收益：主链路可在较少文件内追踪，开发和演示成本低，也避免过早复制未被
使用的抽象。但当需求进入热插拔、跨进程、可靠投递和故障恢复时，这些差异会从“简化”变成必须补的能力。

## 4. 被动链路对照

### 4.1 已对齐或按比例实现

| Akashic | Kirakira | 状态 | 说明 |
| --- | --- | --- | --- |
| `bus/*`、被动 lane | `bus.py`、`event_bus.py` | 对齐 | 同 chat 保序、跨 session 并发、intercept/fanout |
| `agent/looping/*` | `runtime.AgentLoop` | 轻实现 | 消费、session 串行、中断成立，类型与层次更少 |
| `agent/core/passive_turn.py`、`turns/*` | `PassiveTurnPipeline` + `DefaultReasoner` + `turns.py` | 轻实现 | streaming tool loop、retry、持久化成立；TurnResult/副作用已抽出,主动与 Drift 已改用 |
| lifecycle phases | `lifecycle.py` + EventBus + `phase.py` | 轻实现 | 7 个 phase context 与 slot 拓扑排序已有；模块间尚无 frame.slots 传递 |
| prompting/context policy | `prompting/`、`context_builder.py`、`context_policy.py` | 对齐 | PromptBlock、预算预检、分级裁切、trace |
| retrieval pipeline | `retrieval.py` | 对齐 | lexical/vector 多路召回、RRF、热度、注入预算 |
| session manager/store | `session.py` | 轻实现 | JSON canonical + SQLite FTS；存储抽象更薄 |
| tool registry/hooks | `tools/registry.py`、`tool_hooks.py` | 对齐 | schema、pre/post/error、timeout、deferred discovery |
| MCP declarations/generation | `mcp/` + `snapshot.py` | 轻实现 | 声明式发布、失败回滚、在途 turn 固定代际 |
| Channel host | `channels/` | 分项判断 | Telegram 已按 Reference 移植；Web、QQ/OneBot 与官方 QQBot 仍是 Kirakira 实现 |
| subagent/background | `subagent.py` | 轻实现 | inline/background 与并发上限；无 peer-agent 进程体系 |

### 4.2 记忆系统：M0–M2 与 Stage 3/4/5 全部完成

> **更新(2026-07-26)**:命名已从 `memory2` 折叠进 `coremem`,数据库 `memory2.db → coremem.db`。
> 历史见 [MEMORY2_M0_M1.md](./MEMORY2_M0_M1.md),完整架构与当前状态见
> [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md),本文不再重复维护状态表。

引擎(照抄 Reference `DefaultMemoryEngine`)+ DI 缝(`MemoryServices`)+ 被动检索
`engine.query(intent="context")` + 对话后摄入 + 显式工具(schema 由 `engine.tool_profile()`
声明,返回带 `§cited:` 引用协议)+ 主动兴趣检索(`intent="interest"`)均已接线并有测试;
embedding 已实配(1024 维语义召回现场验证)。Drift/主动的 `read_long_term` 绑定 coremem 的
markdown store——Reference 里它读的就是 `MEMORY.md`(`MemoryProfileApi`),不是引擎,早期文档
把它写成"切引擎"是误述。doctor 比对 `coremem/*.py` 与 `Reference/memory2/*.py`,当前 `drifted=[]`。

有意保留的偏离:引擎 Disabled(未配 embedding)时,kirakira 仍注册词法回退版记忆工具,
Reference 则完全不注册;门控在 kirakira 是 `embedding.base_url`,不沿用 Reference 的
light/main 端点回退链(聊天端点通常没有 /embeddings,静默回退只会制造失败请求)。

记忆侧仍未对齐的项(引擎插件路由 `config.memory.engine`、Dashboard 改走 `MemoryAdminApi`、
RecallInspector 观测面板)记录在 [NOW.md](./NOW.md)。

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
| MCP proactive sources | `ProactiveSource` + `FileInboxSource` + `mcp_sources.compile_proactive_sources` | 轻实现 | 插件声明已编译成真实源并进入 SourceRegistry；缺真实 MCP server 端到端证据 |
| source gateway/reservoir | `SourceRegistry` + `ProactiveStateStore` | 轻实现 | 并发 fetch、稳定 id、未读/消费成立；恢复合同更弱 |
| wake hazard/ranking | severity/newness 排序 + Drift hazard 采样到期 | 轻实现 | Drift 侧已有 hazard 与采样到期;主动侧仍无累计 hazard、兴趣 embedding 与 turn prototype 校准 |
| forced tool decision | `ProactiveJudge` 严格 JSON;`ModelClient` 已支持 `tool_choice`,Drift 主循环已用 required+具名强制收尾 | 替代实现(判断)+ 对齐(Drift 收尾) | 判断链路仍是 JSON;能力已具备,切换属行为变更待实弹 |
| `ProactiveKernel` + lifecycle | `ProactiveLoop._tick()` + `proactive/modules.py` + `frame.py` | 轻实现 | 模块流水线,顺序由 slot 依赖图决定,插件可插模块;无 factory 与 start/stop rollback |
| proactive snapshot lease | tick 双租约(plugin generation + runtime snapshot)+ gateway 快照钉定 | 对齐 | tick 中途换代仍用开始时的模块集合与工具代际;kirakira 把 Reference 的一份 snapshot 租约拆成两个对象,故取两份 |
| delivery + pending ACK | Channel receipt + delivery id + pending ACK + deliveries 去重表 | 对齐 | 成功后提交、ACK 队列,以及内容指纹+时间窗的跨崩溃去重(照 Reference proactive_v2/state.py) |
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
2. **跨崩溃不重复,但不是 exactly-once。** 发送前落地投递意图、渠道明确失败才撤销,因此进程在
   渠道成功与本地提交之间崩溃不会重复发送;代价是标记后、发送前崩溃会漏发这一条,语义是
   **至多一次 + 窗口内不重复**。见 [decisions/0004](./decisions/0004-delivery-dedup.md)。
3. **插件源已接线，但缺真实端到端证据。** 插件声明可编译成源并进入 registry；仍没有生产级 RSS/日历 source 的凭据、健康检查和限流。
4. **主动链路没有代际租约。** 被动 turn 的 snapshot 经验尚未延伸到一次完整 proactive tick。
5. **单目标配置。** 当前一套 `[proactive.target]` 对应一个 channel/chat，不是多用户调度器。

这五项比“有没有 phase graph”更影响生产可用性，优先级应更高。

## 6. Drift 对照

| Akashic | Kirakira | 状态 | 说明 |
| --- | --- | --- | --- |
| `plugins/drift_flow` skill 驱动 | `drift/skills.py` + `runner.py` + `proactive/modules.py:DriftModule` | 轻实现 | SKILL.md 决定行为,每次一轮 Agent run;Drift 已是流水线模块而非 hook |
| run/cursor/continuum | `drift.db` | 轻实现 | run 记录、skill continuum、min interval 已有 |
| message/finish tools | `drift/tools.py` | 替代实现 | 线程内生成草稿，主 event loop 等 Channel；成功记 sent，失败记 silent |
| hazard drive | `drift/drive.py` + `drift_schedule` 表 | 轻实现 | 空闲驱动 × 三项抑制 → hazard,并按分布**采样到期时刻**;min_interval 保留为硬下限 |
| journal/self-observation | `drift.db:skill_journal` + `journal_append` 工具 | 轻实现 | append-only journal 按 entry_type 分类,self_observation 跨 skill 汇总并注入 briefing;无 question/reinforce/revise 的结构化条目类型 |
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
| 完整 Dashboard / frontend | 轻实现 | Memory2 已有分页、过滤、详情、编辑、相似项、删除和健康 API；尚无 Reference 完整前端 |
| app server / control protocol | **已对齐**(JSON-RPC/NDJSON/Unix socket) | stdio 版 app_server 与 Dashboard HTTP API 未做 |
| Agent 发起的 restart | **已实现并实弹换代成功**(2026-07-26) | RestartCoordinator + 准入冻结 + supervisor 私有管道提交,见 [design/agent-restart.md](./design/agent-restart.md);rolling backup 未做 |
| peer-agent 进程管理 | 未实现 | 当前 subagent 已满足本地委派范围 |
| 插件 marketplace | 未实现 | 现有 workspace 插件满足开发阶段 |
| 多 Provider backend | 未实现 | 当前只承诺 OpenAI-compatible;`tool_choice`(auto/required/具名)已补上 |

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
- Telegram/启动：固定 Reference 源码字节一致性测试、原始 Telegram utils 契约测试和真实
  Supervisor → gateway readiness 启动。

当前完整离线回归为 `477 passed, 4 subtests passed`(2026-07-26,含 agent_restart、
tick 双租约、tool_choice、scheduler misfire 恢复与记忆工具对齐的新增用例)。

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
