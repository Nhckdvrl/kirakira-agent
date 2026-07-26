# Kirakira 记忆系统(coremem)

> 快照:2026-07-27(akasha 引擎与引擎路由补齐之后)。Reference 固定为 `012e37c8b51df045353972bb551d8e868ab52455`。
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

### 4.3 显式记忆工具(已走引擎)

`memorize / recall_memory / forget_memory` 在引擎承重时走 `engine.mutate(remember/forget)` /
`engine.query(intent=answer/timeline)`(`tools/builtins.py`),未承重时回退旧词法路径。
schema 与描述由 `engine.tool_profile()` 声明(对照 Reference `agent/tools/meta/register.py`),
返回格式对齐 Reference:recall 带 evidence/source_ref/trace 与 `§cited:` 引用协议,
`time_filter` 预设串解析为时间窗,limit 钳制 1..200;memorize 的 `tool_requirement`/`steps`
进 mutation metadata,procedure 记忆靠它们生成 rule_schema。

与 Reference 的显式偏离:Reference 在引擎 Disabled 时不注册记忆工具;kirakira 保留词法
回退注册,使未配 embedding 也能用基础记忆。

### 4.4 两套引擎与路由

`[memory].plugin` 决定用哪套引擎(照 Reference `bootstrap/wiring.resolve_memory_plugin`):

| plugin | 引擎 | 语义 |
| --- | --- | --- |
| `default`(缺省) | `coremem/default_engine.py` | 抽取成条目 → 向量 lane + 关键词 lane → RRF 融合 → 注入预算 |
| `akasha` | `akasha/engine.py` | 把**整轮对话**存成图节点,查询做涟漪扩散(RAR);真相源是 `sessions.db/messages` |

两点必须知道:

- **`[memory].engine` 不是这个键**。那是 M1 迁移的存储 owner 选择器(auto/legacy/coremem),
  语义完全不同,合并会让老 workspace 的 `engine="auto"` 被当成引擎名解析失败。
- **工具面由引擎决定**:default 声明 memorize/recall/forget;akasha 只声明 recall 与自定义
  `reinforce_memory`(它从 turn 自动摄入,本就没有 memorize)。注册器声明什么注册什么,
  不做回退——否则模型会看到一个必然被拒绝的工具。

akasha 依赖 `sessions.db` 的 `messages` 投影表(canonical 仍是 per-session JSON,
投影与 `messages_fts` 随 save 增量维护、启动时全量重建)。实弹记录见
[design/live-verification.md](./design/live-verification.md) 第 12 节。

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
| 被动检索 `engine.query(context)` | ✅ pipeline 已接线 |
| 对话后摄入 `TurnCommitted→ingest` | ✅ 引擎自订阅(承重时生效) |
| 显式工具走引擎 + tool_profile schema | ✅ 见 §4.3;Disabled 时词法回退是显式偏离 |
| 主动 `engine.query(interest)` | ✅ `proactive/loop.py:_interest_hits`(read_only + strong floor) |
| Drift/主动 `read_long_term` | ✅ 绑定 coremem markdown store(Reference 里它读的就是 MEMORY.md,不是引擎) |
| 旧 consolidation 路径 | ✅ 已删,驱动权移交 `MarkdownMemoryMaintenance`(见 decisions/0003) |
| embedding 实配 | ✅ 已配并现场验证(1024 维语义召回);`model` 未配时落 Reference 同款默认 `text-embedding-v3` |
| 旧 `MemoryRuntime` 词法回退栈 | 保留:作为引擎未承重时的检索回退与 subagent/context_builder 依赖 |

## 7. 里程碑

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| M0 差距审计 | 完成 | doctor / Reference pin / 漂移检查 / 契约测试 |
| M1 唯一结构化 owner | 完成 | 迁移 / 回滚 / Dashboard(数据已清空重来) |
| M2 DefaultMemoryEngine | 完成 | 引擎装配、DI 注入、pipeline 消费 |
| Stage 3 embedding | 完成 | 已实配并现场验证;1024 维语义召回可用 |
| Stage 4 主动兴趣检索 | 完成 | content 判断前 `engine.query(intent="interest")` |
| Stage 5 工具切引擎 + consolidation 移交 | 完成 | 工具走 `engine.mutate/query`;旧 consolidation 路径已删 |

全部里程碑完成;引擎插件路由与 akasha 已于 2026-07-27 补齐(见 §4.4)。
后续记忆侧的未完成项只维护在 [NOW.md](./NOW.md)。

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
