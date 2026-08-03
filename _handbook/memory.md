# 记忆系统合同

## 三个不同 owner

- `sessions.db/messages`：逐轮原始对话真相。
- `memory/coremem.db`：Default 结构化长期记忆。
- Markdown 档案：`MEMORY.md`、`SELF.md`、`PENDING.md`、`RECENT_CONTEXT.md`，由 maintenance 与
  人工共同维护。

Runtime 只通过 `MemoryServices.engine` 调用 `query/ingest/mutate`。协议在 `core/memory/`，默认算法
在 `memory2/`，引擎装配在 `plugins/default_memory/` 与 `plugins/akasha/`。不存在第二套
`kirakira_agent/coremem` owner。

## Default 引擎

Default 将对话抽成 profile/preference/procedure/event：向量与关键词多 lane 召回，RRF 按排名融合，
再叠加强化/时间信号并受注入预算约束。写入失败不能固化半索引记录；查询 embedding 失败可以降级
到关键词 lane。普通 forget 是逻辑退休，明确管理操作才可物理删除。

工具 schema 由引擎的 `tool_profile()` 决定：Default 暴露 memorize/recall/forget，并返回
evidence/source_ref/trace。

## Akasha v1

`[memory].plugin="akasha"` 选择当前可用的 v1 图引擎。它以 `sessions.db/messages` 的完整 turn 为
真相，维护 message embedding、turn node 与激活边，查询执行 Dense + Ripple/RAR 召回。它从
`TurnCommitted` 自动摄入，所以只暴露 recall 与 reinforce，不提供必然被拒绝的 memorize。

当前 v1 已验证：在线 embedding、turn 摄入、图召回、持久化证据、强化与模型消费召回内容。Akasha
v2 暂不升级，但 engine/plugin/admin 边界允许以后原位替换，不新增平行架构。

## 配置门控

```toml
[memory]
enabled = true
plugin = "default" # 或 akasha

[memory.embedding]
model = "text-embedding-v3"
base_url = "${EMBEDDING_BASE_URL}"
api_key = "${EMBEDDING_API_KEY}"
```

未配置 embedding 时结构化引擎不承重，pipeline 使用基础词法路径。聊天 completion 端点通常不
等于 embedding 端点。

## 失败边界

| 情况 | 结果 |
| --- | --- |
| 查询 embedding 失败 | 查询可降级关键词；trace 标明 |
| 写入 embedding 失败 | 抛错，不发布半成品 |
| SQLite integrity/schema 损坏 | fail loud，不把旧 JSON 当权威恢复 |
| consolidation 模型失败 | 回复已提交；记录失败，不伪造记忆成功 |
| 删除 Session | 按 source 生命周期退休相关长期记忆 |

管理命令与完整数据流见 [docs/MEMORY_SYSTEM.md](../docs/MEMORY_SYSTEM.md)，评测边界见
[docs/MEMORY_EVALUATION.md](../docs/MEMORY_EVALUATION.md)。
