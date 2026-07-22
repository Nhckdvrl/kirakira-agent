# 记忆系统（当前 M1）

## 先分清三类状态

- **Session**：逐轮对话历史，保存在 `sessions/`，回答“当时说了什么”。
- **结构化长期记忆**：跨会话 profile/preference/procedure/event，唯一 owner 是 `memory/memory2.db`。
- **Markdown 状态**：`MEMORY.md`、`SELF.md`、`PENDING.md` 等各自独立，不由 Memory2 替代。

```text
用户消息
  → PassiveTurnPipeline
  → MemoryRuntime 兼容 façade
  → memory2.db active items
  → lexical/vector lane + RRF + hotness + 注入预算
  → Prompt
  → 回复

显式 memorize / 后台 consolidation
  → 同一个 MemoryRuntime
  → MemoryStore2
  → memory2.db
```

旧 `memory/items.json` 已归档为恢复点，正式 runtime 不再读取或双写它。

## 当前持久化 owner

| 对象 | owner | 当前用途 |
| --- | --- | --- |
| `memory2.db` | `MemoryStore2` | 唯一结构化长期记忆、状态、强化、source_ref |
| `MEMORY.md` | Markdown runtime/人工 | 人工与兼容长期档案，独立于结构化 owner |
| `SELF.md` | Markdown runtime/人工 | Agent 自我模型 |
| `PENDING.md` | Markdown runtime | 待整理事实 |
| `RECENT_CONTEXT.md` | consolidation | 可重建近期摘要 |
| `HISTORY.md` | 当前 legacy consolidation | M4 前仍会写；Reference 四文件链路接入后停止生产写入 |

## M1 已经保证什么

1. `memory2.db` 是唯一结构化 owner，不再把它当 `items.json` 的可忽略镜像。
2. 被动检索、`memorize/recall_memory/forget_memory` 和 Dashboard 读取同一个 owner。
3. 普通遗忘执行逻辑 supersede；只有 Dashboard 明确确认才允许 hard delete。
4. 迁移使用 offline lock、backup、staging DB、`PRAGMA integrity_check`、原子发布和 rollback。
5. 删除 Session 会按 `source_ref` 逻辑退休对应记忆。
6. 写入 embedding 失败 fail-loud；查询 embedding 失败可以降级到 lexical lane。

## 当前检索仍是什么

M1 保留 Kirakira 旧同步接口作为兼容 façade：

```text
query
  ├─ lexical lane（精确实体、路径、错误码）
  └─ vector lane（配置 embedding 后启用）
       → RRF
       → reinforcement/age hotness
       → 字符预算
       → Prompt Context Frame
```

当前 embedding 未配置，因此在线查询只使用 lexical lane。RRF、热度和注入预算仍能工作，但这不是
Reference `DefaultMemoryEngine` 的完整检索语义。

## 当前写入语义

- 精确重复会强化而不是重复插入。
- legacy `identity/fact/requested_memory` 映射为 `profile`，原类型保存在 `extra_json`。
- `forgotten` 映射为 `superseded`。
- M1 仍接受旧工具参数 `content/memory_type`。
- 当前 consolidation 仍是 Kirakira 旧实现；自动抽取、语义去重与 Markdown 更新尚未按 Reference M4
  收口。

因此当前写入可以直接使用，但不能把 M1 描述成已经具备 Reference Memorizer 的 procedure 合并、
preference/profile 替换、显式 evidence 或 PostResponseWorker 失效检测。

## 失败边界

| 情况 | 结果 |
| --- | --- |
| 查询 embedding 失败 | 降级词法，本轮继续 |
| 写入 embedding 失败 | 抛错，不静默固化半索引记录 |
| `memory2.db` integrity/schema 损坏 | fail-loud，不回退旧 `items.json` |
| consolidation 模型失败 | 用户回复已完成；记录错误，不伪造记忆成功 |
| 普通 forget | status → superseded，不物理删除 |
| 明确 hard delete | 仅 Dashboard 确认操作允许 |

## Memory2 长期计划停点

```text
M0 doctor/审计                  完成
M1 Memory2 唯一结构化 owner     完成  ← 当前版本
M2 DefaultMemoryEngine          未开始
M3 被动 context / 主动 interest 未开始
M4 自动提取与四文件 Markdown     未开始
M5 evidence / undo / replacement 未开始
M6 E2E eval 与切除 legacy        未开始
```

下一轮从 M2 开始：先配置真实 embedding、接入 Reference `DefaultMemoryEngine`、Memorizer 和新工具合同，
再切换生产查询。详细迁移、验证和回滚命令见
[`docs/MEMORY2_M0_M1.md`](../docs/MEMORY2_M0_M1.md)。
