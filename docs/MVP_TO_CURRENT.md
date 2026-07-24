# Kirakira Agent：从 MVP 到当前架构

> 快照:2026-07-25。Reference 固定为 `012e37c8b51df045353972bb551d8e868ab52455`。
> 本文只写已进入正式入口并有测试证据的能力;源码存在但没有生产调用点的不算完成。
> 目标定位:**先照 Reference 对齐架构、跑通完整链路(MVP),细节后补。**

## 1. 当前结论

| 范围 | 状态 | 结论 |
| --- | --- | --- |
| 被动回复 | 已跑通,工程化基座 | Web / Telegram / QQ / CLI 进同一 AgentLoop |
| **异步原生 model runtime** | **已对齐(Phase 1)** | 客户端 `acomplete`/`acomplete_stream`,去掉 `to_thread` 阻抗 |
| **记忆引擎 + DI 缝** | **架构已对齐(Phase 0/2)** | `DefaultMemoryEngine` 移植完成,`MemoryServices` 注入,pipeline 走 `engine.query` |
| 主动推送 | MVP 已跑通 | Tick / Source / 判断 / Channel callback / Session / ACK 闭环 |
| Drift | MVP 已跑通 | 空转后执行 `SKILL.md`、用工具、发送并保存连续状态 |
| Telegram / Supervisor | Reference 对齐 | 源文件逐字节一致,差异在文件外 binding |

完整离线回归:

```text
274 passed, 4 subtests passed
```

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

**门控(MVP 关键)**:配了 `[memory.embedding]` → `DefaultMemoryEngine` 承重检索;没配 → `DisabledMemoryEngine`,pipeline 回退旧词法路径。因为 DefaultMemoryEngine 读写都要 embedding,所以"引擎真正承重"依赖 embedding 配置(下一步 Stage 3)。DI 缝、工厂、pipeline 消费服务包这套**架构已经和 Reference 一致**,回退路径是过渡细节。

对照 Reference:`MemoryServices(engine)` = `agent/looping/ports.py`;工厂门控 = `bootstrap/memory.py`(启用→engine,否则 `DisabledMemoryEngine`);pipeline 调 `engine.query` = `agent/retrieval/default_pipeline.py`。

### 2.4 异步原生 model runtime(Phase 1)

**之前**:客户端只有同步 `complete`(urllib + `time.sleep`),runtime 满地 `asyncio.to_thread(model_client.complete)`。这套"同步内核裹线程"让所有 async 插件接口(尤其 `engine.query`)在 seam 上都别扭。

**现在**:客户端加了异步原生 `acomplete` / `acomplete_stream`(httpx),runtime 与 `_compat.provider` 都**优先 await 异步、同步 stub 才回退线程**。memory / proactive / drift 的 LLM 调用现在都是异步原生,不再裹线程——记忆接线感到的"阻抗"从根上没了。SSE 解析在同步/异步两条流之间共用一份,保证一致。

## 3. 与 Reference 的架构对齐:进度与路线

Kirakira ≈2.6 万行,Reference ≈10.5 万行(产品代码)。差距的核心不是"少了功能",而是 **4 个地基抽象需要重构**,其余多是坐在地基上的"加法"。

| 地基(必须重构) | 状态 |
| --- | --- |
| ① 异步原生 model runtime | ✅ Phase 1 完成 |
| ② 依赖注入(Services/Ports) | 🟡 记忆缝已落地(Phase 2),其余子系统待推广 |
| ③ Turn 抽象 + lifecycle slot DAG | ⬜ 未开始 |
| ④ 记忆 seam(引擎藏在干净接口后) | ✅ 检索缝已对齐;工具/摄入全量切换见 Stage 5 |

| 加法(依赖地基,可增量) | 状态 |
| --- | --- |
| control plane / app server | ⬜ 无 |
| 前端 Dashboard | ⬜ 无(仅 Memory 管理 API) |
| peer-agent 进程管理 | ⬜ 无 |
| 主动 plugin/MCP source、durable outbox、多目标 | ⬜ MVP 文件源 |
| 插件市场 | ⬜ workspace 插件 |

**顺序原则**:上层"加法"依赖下层"重构",先地基后上层,才不会"接个东西搞半天"。

## 4. 记忆里程碑进度(细化)

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| M0 差距审计 | 完成 | doctor / Reference pin / 漂移检查 |
| M1 唯一结构化 owner | 完成 | 迁移 / 回滚 / Dashboard(数据现已清空重来) |
| **M2 DefaultMemoryEngine** | **移植完成 + 检索缝已接** | 引擎照抄 Reference,契约测试绿;经 `MemoryServices` 注入 pipeline |
| Stage 3 embedding 配置 | 未开始 | 配 `[memory.embedding]` 后引擎从 Disabled 切到承重;需外部 embedding 端点 |
| Stage 4 主动/Drift 走 `engine.query(interest)` + `read_long_term` | 未开始 | 保住两条链路接口 |
| Stage 5 切除旧栈 | 未开始 | 删旧 `MemoryRuntime` 检索/consolidation、工具改走引擎 |

## 5. 主动推送 / Drift(MVP,未变)

主动:后台 Tick → Gate → `SourceRegistry.fetch_all()` → alert/content/context 去重 → LLM 判断 → 真实 Channel callback → 成功后写 Session + ACK。内置文件 Source(`<workspace>/proactive/inbox/*.jsonl`)。缺 plugin source、durable outbox、多目标、跨崩溃恢复。

Drift:主动空转 → 选 `drift/skills/*/SKILL.md` → 注入记忆/近期/continuum → 复用 Agent + 默认工具 → `message_push`/`finish_drift` → 成功记 sent 否则 silent。缺 Reference 的 journal / self-observation / hazard drive。

## 6. 明确未完成

- 记忆引擎"真正承重"依赖 embedding 配置(Stage 3);当前默认 `DisabledMemoryEngine` + 旧词法回退。
- 显式记忆工具(memorize/recall/forget)仍走旧 `memory.py`,未切引擎;旧栈完整切除是 Stage 5。
- 引擎 closeables 目前未在关停时统一关闭(进程退出兜底),Stage 5 收口。
- 地基 ③(Turn/lifecycle DAG)未开始;control plane / 前端 / peer-agent / 插件市场未做。
- QQ 两渠道能跑,未像 Telegram 逐字节复刻。

## 7. 下一步优先级

1. **Stage 3:配 embedding**,让 `DefaultMemoryEngine` 从 Disabled 切到承重,第一次看到引擎真正做检索/摄入。
2. **Stage 4/5:把工具、主动 interest、Drift long-term 切到引擎,删旧栈**,记忆子系统彻底对齐。
3. **地基 ②推广 + ③开工**:把 Services/Ports 推广到 context/session,再上 Turn 抽象与 lifecycle DAG。
4. 之后才是 control plane / 前端 / 主动 source 等"加法"。

## 8. 文档导航

- [STARTUP_AND_CHANNELS.md](./STARTUP_AND_CHANNELS.md):启动、Telegram、渠道现状。
- [MEMORY2_M0_M1.md](./MEMORY2_M0_M1.md):记忆 M0/M1 owner、迁移、恢复(注:命名已从 memory2→coremem)。
- [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md):与 Reference 的差异审计。
- [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md):主动推送与 Drift。
