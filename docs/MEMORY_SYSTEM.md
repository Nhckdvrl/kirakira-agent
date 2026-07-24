# Kirakira 记忆系统(coremem)

> 快照:2026-07-25。Reference 固定为 `012e37c8b51df045353972bb551d8e868ab52455`。
> 本文是记忆系统的**当前权威文档**。历史迁移细节(M0/M1 owner 切换、备份/回滚)见
> [MEMORY2_M0_M1.md](./MEMORY2_M0_M1.md);本文覆盖当前架构、数据流、配置与对齐状态。

## 1. 一句话定位

Kirakira 的记忆照 Reference `plugins/default_memory` 重建:**一个 `DefaultMemoryEngine` 把所有算法零件
(检索/摄入/HyDE/改写/去重/规则)装配好,对 runtime 只暴露一条干净接口
`engine.query()/ingest()/mutate()`;runtime 通过 `MemoryServices` 依赖注入拿到它,不认识实现。**

命名说明:Reference 里叫 `memory2`(那是 Akashic 自己 memory→memory2 的版本遗留),Kirakira 无此历史,
已整体折叠进单一 `coremem` 包,数据库为 `coremem.db`。

## 2. 包结构

```text
kirakira_agent/coremem/
  engine.py               记忆协议(= Reference core.memory):MemoryQuery / MemoryEngine / 类型
  default_engine.py       DefaultMemoryEngine —— 装配者(照抄 Reference plugins/default_memory/engine.py)
  default_memory_config.py 检索/注入阈值(Reference 默认值)
  services.py             MemoryServices(engine) + build_memory_services 工厂  ← DI 缝
  store.py                MemoryStore2 —— SQLite + sqlite-vec 结构化存储(唯一 owner)
  retriever.py            多路召回 + RRF + 热度 + 注入预算
  memorizer.py            写入 + 语义去重/替换/强化
  embedder.py             OpenAI 兼容 /embeddings 客户端(远端 API)
  hyde_enhancer.py / query_rewriter.py / query_builder.py / sufficiency_checker.py
  procedure_tagger.py / rule_schema.py / profile_extractor.py / post_response_worker.py
  dedup_decider.py / injection_planner.py / models.py
  markdown.py             四文件人工长期档案(MEMORY.md/SELF.md/PENDING.md/RECENT_CONTEXT.md)
  events.py / utils.py    TurnIngested / ConsolidationCommitted 等
数据文件:<workspace>/memory/coremem.db  +  四份 Markdown
```

对照 Reference:`coremem/engine.py`=`core/memory/engine.py`;`coremem/default_engine.py`=
`plugins/default_memory/engine.py`;`coremem/services.py`≈`agent/looping/ports.py:MemoryServices`
+`bootstrap/memory.py`;其余零件=`memory2/*`。

## 3. 架构:引擎 + DI 缝 + 存储

```text
                 ┌─────────────────── MemoryServices(engine) ── DI 缝 ───┐
 PassiveTurn ────┤                                                       │
 Proactive   ────┤   runtime 只调 engine.query / ingest / mutate         │
 Drift       ────┤                                                       │
                 └───────────────────────┬───────────────────────────────┘
                                         ▼
                              DefaultMemoryEngine(装配者)
        ┌──────────────┬──────────────┬───────────────┬────────────────┐
     Retriever      Memorizer      Embedder       ProcedureTagger   PostResponseWorker
        │              │              │               │                │
        └──────────────┴──────► MemoryStore2 (coremem.db) ◄────────────┘
```

- **runtime 不认识零件**:HyDE、改写、多路召回、RRF、语义去重、注入预算全在引擎内部,对外只有稳定协议
  `MemoryQuery → MemoryQueryResult`。换引擎实现只需换 `MemoryServices.engine`,不改任何调用点。
- **存储唯一 owner**:`MemoryStore2`(SQLite),逻辑退休(superseded)而非物理删除,支持 replacement/undo。
- **Markdown 四文件**独立于结构化存储:人工长期档案 + 自我模型 + 待整理 + 近期投影,供 Drift `read_long_term`
  和 consolidation 使用(Reference 也这样分)。

## 4. 数据流

### 4.1 被动检索(已接线)

```text
PassiveTurnPipeline
  → memory_services.engine.query(MemoryQuery(intent="context", text=用户输入, scope=会话作用域))
  → 引擎:HyDE 假想 / query 改写 / 向量 lane + 关键词 lane → RRF 融合 → 热度加权 → 注入预算
  → MemoryQueryResult.text_block → 注入上下文 Frame → 模型
```

runtime 侧只有一句 `await engine.query(...)`(对照 Reference `agent/retrieval/default_pipeline.py`)。
能力门控 `_engine_can_retrieve`:引擎具备 `RETRIEVE_CONTEXT_BLOCK` 能力才走它,否则回退旧词法路径。

### 4.2 对话后摄入(事件驱动)

```text
turn 提交 → TurnCommitted 事件
  → 引擎自订阅(构造时传入 event_bus)→ enqueue TurnIngested
  → PostResponseMemoryWorker:抽取候选事实 → 语义去重/替换 → 写入 MemoryStore2
```

主回复链路不等待摄入(异步后处理)。这条在引擎承重时自动生效。

### 4.3 显式记忆工具

`memorize / recall_memory / forget_memory` 目标是走 `engine.mutate(remember/forget)` /
`engine.query(intent="answer")`。**当前过渡态**:这三个工具仍走旧 `memory.py`,切引擎属 Stage 5。

## 5. 门控与 embedding 配置(关键)

`build_memory_services` 的门控(对照 Reference `bootstrap/memory.py`):

```text
memory.enabled 且配了 [memory.embedding].base_url  → DefaultMemoryEngine(承重)
否则                                               → DisabledMemoryEngine(检索为空,pipeline 回退旧词法)
```

**为什么依赖 embedding**:`DefaultMemoryEngine` 读(检索向量 lane)和写(摄入前 embed)都要向量,所以引擎"真正
承重"需要一个 embeddings 端点。Reference 用 **DashScope `text-embedding-v3`**(OpenAI 兼容,远端 API,非本地):

```toml
[memory.embedding]
model = "text-embedding-v3"
api_key = "sk-..."
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
```

- 任意 OpenAI 兼容 `/embeddings` 端点都可(OpenAI text-embedding-3、SiliconFlow bge 等)。
- 聊天模型(如 deepseek)通常**没有** embeddings 端点,需单独配。
- 向量维度默认 `VEC_DIM=1024`,`output_dimensionality` 可覆盖;维度必须与库内已有向量一致。
- kirakira 的 `Embedder`/`build_config` 与 Reference 协议一致,**填好配置即自动从 Disabled 切到承重,无需改代码**。

## 6. 与 Reference 的对齐状态

| 维度 | 状态 |
| --- | --- |
| 引擎本体 `DefaultMemoryEngine` | ✅ 照抄移植,契约测试 `test_coremem_engine_contract.py` 绿 |
| DI 缝 `MemoryServices` + 工厂 | ✅ 对齐 `ports.py` + `bootstrap/memory.py` |
| 被动检索 `engine.query(context)` | ✅ pipeline 已接线(承重需 embedding) |
| 对话后摄入 `TurnCommitted→ingest` | ✅ 引擎自订阅(承重时生效) |
| 显式工具走引擎 | ⬜ 过渡态仍走旧 `memory.py`(Stage 5) |
| 主动 `engine.query(interest)` / Drift `read_long_term` | ⬜ 保住接口,Stage 4 切换 |
| 旧 `MemoryRuntime` 检索/consolidation 栈 | ⬜ Stage 5 删除 |
| embedding 实配 | ⬜ Stage 3(需外部端点) |

## 7. 里程碑

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| M0 差距审计 | 完成 | doctor / Reference pin / 漂移检查 / 契约测试 |
| M1 唯一结构化 owner | 完成 | 迁移 / 回滚 / Dashboard(数据已清空重来) |
| M2 DefaultMemoryEngine | **移植完成 + 检索缝已接** | 引擎装配、DI 注入、pipeline 消费 |
| Stage 3 embedding | 未开始 | 配 `[memory.embedding]` → 引擎承重 |
| Stage 4 主动/Drift 切引擎 | 未开始 | 保住 interest / read_long_term 接口 |
| Stage 5 切除旧栈 | 未开始 | 工具改走引擎、删旧检索/consolidation |

## 8. 管理与现场检查

```bash
uv run python main.py memory doctor    # 只读:依赖 / import / Reference 漂移 / owner / SQLite / 向量
uv run python main.py memory backup     # 备份
uv run python main.py memory migrate    # 迁移(需 Supervisor 停止,离线独占锁)
uv run python main.py memory verify      # 校验
uv run python main.py memory rollback --backup-id <id>
```

`doctor` 的 Reference 漂移审计比对 `coremem/*.py` 与 `Reference/memory2/*.py`(normalizer 先把 coremem 的
`events/utils` 还原成 `core.memory.*`,其余还原成 `memory2.*` 再逐字节比对),因此保留了 Reference 保真。
