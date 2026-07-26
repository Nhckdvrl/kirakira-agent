# Kirakira Agent：从 MVP 到当前架构

> 快照:2026-07-25(四个地基完成、主动链路 lifecycle 化之后)。Reference 固定为 `012e37c8b51df045353972bb551d8e868ab52455`。
> 本文只写已进入正式入口并有测试证据的能力;源码存在但没有生产调用点的不算完成。
> 目标定位:**先照 Reference 对齐架构、跑通完整链路(MVP),细节后补。**

## 1. 当前结论

| 范围 | 状态 | 结论 |
| --- | --- | --- |
| 被动回复 | 已跑通,工程化基座 | Web / Telegram / QQ / CLI 进同一 AgentLoop |
| **异步原生 model runtime** | **已对齐(Phase 1)** | 客户端 `acomplete`/`acomplete_stream`,去掉 `to_thread` 阻抗 |
| **记忆引擎 + DI 缝** | **已对齐** | `DefaultMemoryEngine` + `MemoryServices`,检索/工具/兴趣检索都走引擎;embedding 已实配 |
| **插件扩展体系** | **骨架已对齐** | 声明式规格、作业/服务 host、代际租约、热重载、安装免重启、slot 依赖图 |
| **Turn 抽象 + 相位 slot 图** | **已对齐** | `TurnResult` 副作用提交单点;`phase.py` 拓扑排序 |
| 主动推送 | MVP 已跑通 | Tick / Source / 判断 / Channel callback / Session / ACK 闭环 |
| Drift | MVP 已跑通 | 空转后执行 `SKILL.md`、用工具、发送并保存连续状态 |
| Telegram / Supervisor | Reference 对齐 | 源文件逐字节一致,差异在文件外 binding |
| **控制面** | **已跑通** | JSON-RPC 2.0 over NDJSON;起 turn / 中断 / 观测 / 排空插件 |

完整离线回归:

```text
494 passed, 4 subtests passed
```

2026-07-26 新增:agent_restart 换代链路、主动 tick 双代际租约、`tool_choice`
(Drift 强制收尾)、scheduler misfire 恢复、记忆工具面对齐(tool_profile 驱动 +
`§cited:` 引用协议)、MCP 工具结果 100k 钳制、**五面板 Web 仪表盘**(总览/记忆/会话/
插件与代际/主动与 Drift)与重做的聊天页。

离线回归之外的**真实模型/真实渠道**验证结果单独记在
[design/live-verification.md](./design/live-verification.md):哪些链路真跑过、哪些只是测过。

## 2. 从 MVP 到当前的被动链路(重点看这里)

这一节把"最短闭环 → 当前结构"讲清楚,尤其是记忆和模型调用两处从"能用"升到"照 Reference 对齐"的变化。

### 2.1 起点:Function Calling MVP

最初只验证最短闭环:

```text
User → Model 选工具 → Tool 执行 → 结果回填 Model → Final Text
```

证明了"能调工具",但没有持久会话、并发、真实渠道、主动触发、长期记忆。

### 2.2 被动链路基座

补齐成当前共享基座:

```text
Channel 入站 → InboundMessage → MessageBus
  → AgentLoop(同 session 串行,跨 session 并行)
  → PassiveTurnPipeline
      → 记忆检索(见 2.3)+ Prompt/Context 预算
      → 模型 + 工具循环(见 2.4)
      → Session commit → TurnCommitted 事件
  → OutboundMessage → 原 Channel
```

这一层有 ToolRegistry/Executor、Hook、MCP 快照代际、Session、Context budget、Streaming、Plugin、Subagent、Schedule、多渠道。

### 2.3 记忆:从"兼容 façade"升到"DefaultMemoryEngine + DI 缝"

**之前(M1 兼容 façade)**:`memory2` 是一堆算法零件被复制进来,但没有装配者;被动 turn 调旧 `MemoryRuntime.retrieve`(同步词法),真正的引擎语义(intent 分流、HyDE、改写、证据、语义去重、自动失效、对话后异步摄入)全缺。这就是"零件都在,却还停在 M1"的根因——**缺的是把零件装起来的 engine**。

**现在(照 Reference 对齐)**:

```text
memory2/ 已整体折叠进 coremem/(单一记忆包,不再叫 memory2)
  coremem/engine.py            记忆协议(= Reference core.memory)
  coremem/default_engine.py    DefaultMemoryEngine(照抄 Reference plugins/default_memory)
  coremem/store.py … retriever/memorizer/embedder/hyde/…  算法零件
  coremem/services.py          MemoryServices(engine)  ← DI 缝
数据库 coremem.db(不再叫 memory2.db)
```

被动 turn 现在这样拿记忆(和 Reference 一致的一条缝):

```text
PassiveTurnPipeline
  → memory_services.engine.query(MemoryQuery(intent="context", scope=…))
  → 引擎内部:HyDE / 改写 / 多路召回 / RRF / 注入预算(runtime 不感知)
  → text_block 注入上下文
```

**门控**:配了 `[memory.embedding]` → `DefaultMemoryEngine` 承重检索;没配 → `DisabledMemoryEngine`,pipeline 回退旧词法路径(因为引擎读写都要向量)。当前已实配并验证:写入"用户偏好用中文回复"后,用几乎无共同词的"我应该用什么语言回复?"能召回——向量 lane 在工作,不是词法。

对照 Reference:`MemoryServices(engine)` = `agent/looping/ports.py`;工厂门控 = `bootstrap/memory.py`(启用→engine,否则 `DisabledMemoryEngine`);pipeline 调 `engine.query` = `agent/retrieval/default_pipeline.py`。

### 2.4 异步原生 model runtime(Phase 1)

**之前**:客户端只有同步 `complete`(urllib + `time.sleep`),runtime 满地 `asyncio.to_thread(model_client.complete)`。这套"同步内核裹线程"让所有 async 插件接口(尤其 `engine.query`)在 seam 上都别扭。

**现在**:客户端加了异步原生 `acomplete` / `acomplete_stream`(httpx),runtime 与 `_compat.provider` 都**优先 await 异步、同步 stub 才回退线程**。memory / proactive / drift 的 LLM 调用现在都是异步原生,不再裹线程——记忆接线感到的"阻抗"从根上没了。SSE 解析在同步/异步两条流之间共用一份,保证一致。

### 2.5 控制面:第四个入口

前面三节讲的都是**消息怎么进来**。还有一条不产生消息的入口:

```text
外部程序 ──JSON-RPC over NDJSON──→ .kirakira/control.sock (0600)
    → ConversationRuntime(每 thread 至多一个 active turn)
    → 同一个 PassiveTurnPipeline(dispatch_outbound=False)
    → 事件流实时回给调用方,不产生 OutboundMessage
```

它与渠道链路**完全并行**:thread id 是 `programmatic:<uuid>`,与
`telegram:123` 天然不同名,不会串台。用途是在不打断真实用户的前提下
观测、驱动、中断。详见 [design/control-plane.md](./design/control-plane.md)。

## 3. 一张图看清当前形态

```text
                      ┌──────────────────────────────────────┐
  Telegram / QQ /     │            MessageBus                │
  QQBot / Web / CLI ─→│  (同 session 保序,跨 session 并发)   │
                      └───────────────┬──────────────────────┘
                                      ↓
  control.sock ──→ ConversationRuntime ─┐
  (programmatic)                        ↓
                          ┌─────────────────────────────┐
                          │    PassiveTurnPipeline      │
                          │  BeforeTurn → BeforeReasoning│
                          │  → PromptRender → Reasoner   │
                          │  → ToolExecutor(hook/超时)  │
                          │  → AfterReasoning → commit   │
                          └──────┬───────────────┬───────┘
                                 ↓               ↓
                       MemoryServices      ToolRegistry
                       (engine.query)      (+MCP 快照代际)
                                 ↓               ↓
                          coremem.db      PluginManager
                                          (per-plugin 代际+租约)

  ProactiveLoop(后台时钟,与 AgentLoop 并列)
    energy → 模块流水线 gate→fetch→ingest→judge→alert→content→drift
    → TurnResult 单点提交(含跨崩溃去重)→ 原 Channel
```

四条竖线是四个地基的位置:模型调用(异步原生)、服务注入(Services/Ports)、
一轮的提交(TurnResult + slot 图)、记忆(engine seam)。

## 4. 与 Reference 的架构对齐:进度与路线

Kirakira ≈3.4 万行,Reference ≈10.5 万行(产品代码)。差距的核心不是"少了功能",而是 **4 个地基抽象需要重构**,其余多是坐在地基上的"加法"。

| 地基(必须重构) | 状态 |
| --- | --- |
| ① 异步原生 model runtime | 完成 |
| ② 依赖注入(Services/Ports) | `ports.py` 分开配置与服务对象,pipeline 消费 SessionServices/ContextServices/MemoryServices |
| ③ Turn 抽象 + lifecycle slot DAG | 完成(模块 frame 签名待迁移,见 NOW.md 第 3 项) |
| ④ 记忆 seam(引擎藏在干净接口后) | 完成 |

| 加法(依赖地基,可增量) | 状态 |
| --- | --- |
| control plane | **已完成**(JSON-RPC over NDJSON,见 [design/control-plane.md](./design/control-plane.md));app server / 前端未做 |
| **agent_restart 换代** | **已完成**(准入冻结 + 双条件提交 + supervisor 握手,见 [design/agent-restart.md](./design/agent-restart.md));真实换代待实弹 |
| **主动 tick 代际租约** | **已完成**(plugin generation + snapshot 双租约,gateway 快照钉定) |
| **前端仪表盘** | **已完成**(五面板:总览/记忆/会话/插件与代际/主动与 Drift + 重做聊天页);零依赖 stdlib,不引构建链 |
| peer-agent 进程管理 | 无 |
| 插件/MCP 主动源 | **已接线**(插件声明→编译→SourceRegistry);真实 MCP 端到端验证见 NOW.md 1.4 |
| 跨崩溃投递去重 | **已完成**(deliveries 表,见 [decisions/0004](./decisions/0004-delivery-dedup.md));多目标调度未做 |
| 插件安装/升级/卸载免重启 | **已完成**;包元数据与非 git 源未做 |

**顺序原则**:上层"加法"依赖下层"重构",先地基后上层,才不会"接个东西搞半天"。

## 5. 记忆里程碑进度(细化)

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| M0 差距审计 | 完成 | doctor / Reference pin / 漂移检查 |
| M1 唯一结构化 owner | 完成 | 迁移 / 回滚 / Dashboard(数据现已清空重来) |
| **M2 DefaultMemoryEngine** | **移植完成 + 检索缝已接** | 引擎照抄 Reference,契约测试绿;经 `MemoryServices` 注入 pipeline |
| Stage 3 embedding 配置 | 完成 | 已实配并现场验证:1024 维,语义召回可用 |
| Stage 4 主动兴趣检索 | 完成 | content 判断前 `engine.query(intent="interest", read_only, strong)` |
| Stage 5 工具切引擎 | 完成 | memorize/recall/forget 走 `engine.mutate/query`;`coremem.db` 单 owner;关停释放资源 |
| Stage 5 收尾:consolidation 移交 | 完成 | 归档由 `MarkdownMemoryMaintenance` 驱动,guard 改用可等待的 `consolidate(force=True)`;见 [decisions/0003](./decisions/0003-consolidation-handover.md) |

## 6. 主动推送 / Drift

主动:后台 Tick 跑一条**模块流水线**(gate → fetch → ingest → judge_context → alert → content → drift),顺序由各模块 `requires` 依赖图决定,插件可声明依赖后插进中间;详见 [design/proactive-lifecycle.md](./design/proactive-lifecycle.md)。投递走 `TurnResult` 单一提交点,含跨崩溃去重。内置文件 Source 与插件声明源都进同一 registry。缺多目标调度与 tick 代际租约。

Drift:现在是流水线上的 `proactive.drift` 模块。触发由 **hazard 采样到期**决定(空闲驱动 × 内容/近期/重复抑制),不再是固定 min_interval;跨轮连续性有 continuum + **append-only journal 与自我观察**;投递走 `TurnResult`,成功记 sent 否则 silent。

## 7. 明确未完成

未完成事项、接手点与验收边界统一维护在 [NOW.md](./NOW.md),本文不重复列举。

摘要(2026-07-26):功能侧——agent_restart 真实换代实弹、渠道 turn 发起重启、
tool_choice 真实模型顺从度、插件源端到端、主动限流/审计厚度;结构侧(按用户指示推迟)——
主动服务化、PhaseFrame 迁移、QQ 逐字节对齐、前端 / peer-agent / eval。

## 8. 文档导航

- [ENGINEERING_METHOD.md](./ENGINEERING_METHOD.md):**怎么把 MVP 养成不塌的系统**——判断信号、五种腐坏方式、验证纪律。
- [INDEX.md](./INDEX.md):文档索引与阅读顺序。
- [NOW.md](./NOW.md):未完成工作与接手点。
- [design/live-verification.md](./design/live-verification.md):实弹验证记录与未验证边界。
- [design/control-plane.md](./design/control-plane.md):控制面分层、turn 状态机与认证。
- [PLUGIN_SYSTEM.md](./PLUGIN_SYSTEM.md):插件声明、代际、热重载、安装。
- [decisions/](./decisions/):架构选择的理由与替代方案。
- [design/](./design/):单次重构的调用链、失败语义与验收。
- [STARTUP_AND_CHANNELS.md](./STARTUP_AND_CHANNELS.md):启动、Telegram、渠道现状。
- [MEMORY2_M0_M1.md](./MEMORY2_M0_M1.md):记忆 M0/M1 owner、迁移、恢复(注:命名已从 memory2→coremem)。
- [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md):与 Reference 的差异审计。
- [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md):主动推送与 Drift。
