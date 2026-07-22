# Memory2 M0–M1：当前可用架构、迁移与恢复

> Reference 基线固定为 `012e37c8b51df045353972bb551d8e868ab52455`。本文只描述已经进入
> Kirakira 正式运行入口的 M0/M1；M2 的 `DefaultMemoryEngine`、Reference 检索阈值和自动失效逻辑
> 尚未启用，不能因为源码存在就声称完成。

## 1. 当前结论

M1 已经完成结构化记忆 owner 切换：

```text
结构化长期记忆：memory/memory2.db       唯一 owner
人工长期档案：    memory/MEMORY.md        独立 owner
自我模型：        memory/SELF.md          独立 owner
待整理事实：      memory/PENDING.md       独立 owner
近期摘要：        memory/RECENT_CONTEXT.md 可重建投影
```

`memory/items.json` 在成功迁移后被改名为只读 `items.legacy.<backup-id>.json`。正式 runtime 不再读取、
写入或双写它。旧镜像 `memory2.db` 也不会被信任；迁移始终从迁移前权威 `items.json` 构建全新 staging
数据库，校验后才原子替换。

首次 M1 迁移演练的结果是：8 条全部保留，4 条 active、4 条 superseded，SQLite
`integrity_check=ok`，逐条字段差异为 0；迁移恢复点是
`20260723-045007-pre-m1-61882d93`。随后用户明确执行过全量记忆与 Session 清理，恢复点为
`20260723-050329-pre-clear-a511979f`。因此上述 8 条是迁移证据，不是当前在线条目数量；在线库会随真实
对话继续变化，状态应通过 doctor 或 Dashboard 查询，不在文档中固化。

## 2. M1 真实数据流

### 2.1 被动回复读取

```text
Web / Telegram / QQ 入站
  → PassiveTurnPipeline
  → MemoryRuntime.retrieve() 兼容 façade
  → 从 memory2.db 读取 active item
  → 现有 lexical/vector lane + RRF + 热度 + 注入预算
  → retrieved_memory Context Frame
  → 模型与工具循环
  → 原 Channel 回复
```

兼容 façade 保留了当前 pipeline 的同步接口，因此 M1 不需要同时重写整个被动运行时。关键差别是
候选事实已经只从 `memory2.db` 加载；`items.json` 不再是隐藏的事实源。

### 2.2 显式记忆工具

```text
memorize(content, memory_type)
  → MemoryRuntime.memorize()
  → legacy type 映射为 procedure/preference/event/profile
  → MemoryStore2.upsert_item()
  → 精确重复强化，否则插入 canonical item

recall_memory(...)
  → 同一 MemoryRuntime
  → 同一 memory2.db

forget_memory(ids)
  → MemoryStore2.mark_superseded_batch()
  → 逻辑退休，不物理删除
```

M1 仍接受旧工具参数 `content/memory_type`，这是有意的过渡层；Reference 新工具 schema 和
`DefaultMemoryEngine` 语义属于 M2。

### 2.3 Dashboard

Web Channel 暴露的是同一个 `MemoryStore2`，不是另外一份缓存：

浏览器页面：`http://127.0.0.1:6322/memory`。

- `GET /api/memories`：分页、搜索、type/status/source/scope/embedding 过滤和排序。
- `GET /api/memory?id=...`：详情。
- `GET /api/memory/similar?id=...`：有向量时查看相似项。
- `PATCH /api/memory`：编辑正文和类型，同时更新 content hash 与向量投影。
- `DELETE /api/memory?id=...`：默认逻辑 supersede。
- `DELETE /api/memory?id=...&hard=true&confirm=HARD_DELETE`：明确确认后物理删除。
- `POST /api/memories/delete`：批量物理删除，同样要求 `confirm=HARD_DELETE`。
- `GET /api/memory/health`：只读 doctor 健康信息。

## 3. 类型与状态迁移合同

| legacy | Memory2 | 附加信息 |
| --- | --- | --- |
| `identity` | `profile` | `extra_json.legacy_memory_type=identity` |
| `fact` | `profile` | `extra_json.legacy_memory_type=fact` |
| `requested_memory` | `profile` | `extra_json.legacy_memory_type=requested_memory` |
| `procedure/preference/event/profile` | 原样 | procedure 补 `rule_schema` |
| `forgotten` | `superseded` | 普通遗忘保持逻辑删除 |

ID、正文、source_ref、reinforcement、created_at、updated_at 和已有 embedding 会逐条保留。迁移遇到
重复 ID、迁移后重复 content hash、坏 JSON、混合 embedding 维度或 SQLite 校验失败时会在 staging
阶段终止，不发布 owner 标记。

## 4. 管理命令

所有命令都通过与正式服务相同的根入口运行：

```bash
uv run python main.py memory doctor
uv run python main.py memory backup
uv run python main.py memory migrate
uv run python main.py memory verify
uv run python main.py memory rollback --backup-id <backup-id>
```

`doctor` 是严格只读操作，检查依赖、模块 import、Reference pin、16 个 Memory2 算法文件的源码漂移、
owner、SQLite schema/integrity/count、向量数量、embedding 配置和四份 Markdown。

`migrate` 和 `rollback` 要求 Supervisor 已停止，并使用独占离线锁。`migrate` 的顺序固定为：

```text
offline check
  → 完整备份
  → 读取 items.json
  → 构建全新 staging DB
  → 逐条映射
  → PRAGMA integrity_check
  → 原子替换 memory2.db
  → 归档 items.json
  → 原子发布 structured-owner.json
  → 逐条 verify
```

`rollback` 在恢复旧备份前还会再保存一份 `pre-rollback` 安全备份，然后将 owner 标记为 legacy。
因此回滚演练不会覆盖掉迁移后的状态。

## 5. 启动与现场检查

正常启动不增加新入口：

```bash
uv run python main.py
```

启动后检查：

```bash
curl -fsS http://127.0.0.1:6322/health
curl -fsS 'http://127.0.0.1:6322/api/memories?page=1&page_size=20&status=active'
curl -fsS http://127.0.0.1:6322/api/memory/health
```

2026-07-23 的真实现场 smoke 已通过：Supervisor readiness 成功；Web 被动请求返回 `M1_OK`；
Dashboard 能读取 active Memory2 item；之后 `verify` 仍为 `ok=true`，并确认 `memory/items.json`
没有被重新生成。用户执行清理后继续通过 Telegram 对话，新的 profile/preference 记忆能够直接写入
同一个 `memory2.db`，证明正式框架确实调用了 M1 owner，而不是只在迁移命令中可见。

## 6. M1 明确尚未完成的部分

- 当前 embedding 未配置，在线条目没有向量；M1 允许空向量，M2 才提供配置向导和 backfill。
- 当前检索仍是 Kirakira 兼容 façade 的旧检索器；Reference 的类型阈值、scope、answer/timeline、
  evidence/citation 和两路假想记忆查询属于 M2/M3。
- 主动兴趣判断和 Drift 当前仍读取独立的 `MEMORY.md/RECENT_CONTEXT.md`；切到
  `engine.query(intent="interest")` 属于 M3。
- 当前 consolidation 仍是旧实现，并继续写 `HISTORY.md`；Reference 四文件 Markdown runtime、
  `ConsolidationCommitted` 和自动长期事实提取属于 M4。
- `QueryRewriter/HyDEEnhancer/SufficiencyChecker/ProfileFactExtractor` 只保留源码，不进入生产主链。

这些限制不是故障降级，而是 M1 与后续里程碑之间的明确边界。

## 7. 长期计划当前停点

| 里程碑 | 状态 | 发布边界 |
| --- | --- | --- |
| M0 | 完成 | 只读 doctor、Reference pin、基础契约测试 |
| M1 | 完成 | 唯一结构化 owner、迁移/回滚、兼容 façade、Dashboard |
| M2 | 未开始 | DefaultMemoryEngine、Memorizer、新工具合同、embedding backfill |
| M3 | 未开始 | 被动 context 与主动 interest 全部切换到 `engine.query()` |
| M4 | 未开始 | 四文件 Markdown、自动提取、失效检测、optimizer |
| M5 | 未开始 | 证据回源、撤销/replacement、并发与 hard-delete 语义 |
| M6 | 未开始 | LongMemEval、状态 oracle、删除全部 legacy 兼容链路 |

因此当前版本可以直接使用 M1 结构化存储，但不能宣称已经对齐 Memory2 的检索、强化、替换、自动提取
和恢复算法。下一轮从 M2 开始。
