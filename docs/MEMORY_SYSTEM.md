# Kirakira 记忆系统

> 当前状态:2026-08-04。Reference head 为 `af49848937c4b62abb2f40a7d91b5f90ea71be6d`；
> 本文描述 Kirakira 现行记忆实现。包边界已与 Reference 对齐；实现深度和 Akasha V2 能力差距见
> [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md)。本文是记忆系统的**当前权威文档**；过期的
> M0/M1 阶段流水账已经删除，迁移/备份/回滚合同以代码、管理命令与 decision 为准。

## 1. 一句话定位

Kirakira 的默认记忆基于 Reference `plugins/default_memory` 的边界重建:**一个 `DefaultMemoryEngine` 把现有算法零件
(检索/摄入/查询构建/去重/规则)装配好,对 runtime 只暴露一条干净接口
`engine.query()/ingest()/mutate()`;runtime 通过 `MemoryServices` 依赖注入拿到它,不认识实现。**

数据库沿用 Kirakira 的 `coremem.db` 文件名；代码不再折叠成私有 `coremem` 包，而是按协议、
算法和插件实现三层归位。

## 2. 包结构

```text
core/memory/               稳定协议、事件、Markdown 档案、runtime 与 MemoryServices
memory2/                   store/retriever/memorizer/embedder/query builder 等算法零件
plugins/default_memory/    DefaultMemoryEngine、配置、插件入口与检索 inspector
plugins/akasha/            Akasha 引擎、图存储、回放与插件入口
session/embedding_store.py 会话 embedding owner
bootstrap/memory_admin.py  doctor/migrate/verify/rollback 命令

数据文件:<workspace>/memory/coremem.db + Markdown 档案
```

这些目录均是可导入、被真实调用的 owner；旧 `kirakira_agent/coremem/` 已删除。

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
  → 引擎:查询构建 / 向量 lane + 关键词 lane → RRF 融合 → 热度加权 → 注入预算
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
| `default`(缺省) | `plugins/default_memory/engine.py` | 抽取成条目 → 向量 lane + 关键词 lane → RRF 融合 → 注入预算 |
| `akasha` | `plugins/akasha/engine.py` | 把**整轮对话**存成图节点,查询做涟漪扩散(RAR);真相源是 `sessions.db/messages` |

两点必须知道:

- **`[memory].engine` 不是这个键**。那是 M1 迁移的存储 owner 选择器(auto/legacy/coremem),
  语义完全不同,合并会让老 workspace 的 `engine="auto"` 被当成引擎名解析失败。
- **工具面由引擎决定**:default 声明 memorize/recall/forget;akasha 只声明 recall 与自定义
  `reinforce_memory`(它从 turn 自动摄入,本就没有 memorize)。注册器声明什么注册什么,
  不做回退——否则模型会看到一个必然被拒绝的工具。

akasha 依赖 `sessions.db` 的权威 `messages` 表；消息使用稳定 UUID 与单调 seq，`messages_fts` 随
append/delete 事务增量维护。旧 per-session JSON 只在首次迁移时读取，此后是非权威镜像。实弹记录见
[design/live-verification.md](./design/live-verification.md) 第 12、14 节。

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

## 6. 当前实现状态

下表是 Kirakira 当前功能状态。Reference 已换成 Akasha V2；Kirakira 当前继续使用已验证可用的
Akasha v1，这是明确延期，不影响现有 engine/plugin/admin 扩展边界。

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

全部里程碑完成；2026-08-04 又用已保存的真实模型和 embedding 配置验证了 Akasha v1 的 turn 摄入、
召回、证据和模型消费。后续记忆侧延期项只维护在 [NOW.md](./NOW.md)。

零成本 Akasha/Memory2 双引擎摄入、检索和持久化 Gate 见
[MEMORY_EVALUATION.md](./MEMORY_EVALUATION.md)。它不调用外部模型，也不等价于真实模型 QA 准确率。

## 8. 管理与现场检查

```bash
uv run python main.py memory doctor    # 只读:依赖 / import / owner / SQLite / 向量
uv run python main.py memory backup     # 备份
uv run python main.py memory migrate    # 迁移(需 Supervisor 停止,离线独占锁)
uv run python main.py memory verify      # 校验
uv run python main.py memory rollback --backup-id <id>
uv run python main.py memory repair-kinds [--dry-run]   # 归一非规范 memory_type
```

**`repair-kinds` 解决什么**:注入选择器只接受 `event/profile/preference/procedure`
(`retriever._select_injection_sections` 的 `else: continue`);旧工具 schema 写入的
`identity/fact/requested_memory` 即使被检索命中也**永远进不了上下文,且完全静默**。
写入边界已归一,本命令修存量:只改 `memory_type` 一列,改前自动备份,`--dry-run` 只报告。
`doctor` 的 `coremem.non_injectable_types` 用来发现这类数据。

`doctor` 只检查 Kirakira 自己的运行时与 workspace，不读取外部源码 checkout。上游差异审计是开发流程，
不是生产 memory doctor 的可用性前提。
