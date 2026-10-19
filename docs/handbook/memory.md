# 使用记忆系统

## 选择引擎

```toml
[memory]
enabled = true
plugin = "default" # 或 akasha / akasha2

[memory.embedding]
model = "text-embedding-v3"
base_url = "${EMBEDDING_BASE_URL}"
api_key = "${EMBEDDING_API_KEY}"
```

聊天 completion 端点通常不提供 embedding，需要单独配置。

## 引擎的工具面

| 引擎 | 适合什么 | 写入方式 | 工具 |
| --- | --- | --- | --- |
| `default` | 稳定事实、偏好、流程、事件 | 从对话抽取结构化 item，也可显式写入 | memorize / recall / forget |
| `akasha` | 保留完整对话关系和联想 | `TurnCommitted` 后自动摄入完整 turn | recall / reinforce |
| `akasha2` | 图状态与消费绑定的快照恢复；显式 opt-in | 已提交原始 turn 的持久 publication | recall；当前不提供显式强化/遗忘 |

Akasha v1 当前可用，已通过真实 embedding、turn 摄入、图/RAR 召回和模型消费验证。它没有
`memorize` 是设计结果，不是工具漏注册。

## 三类数据不要混淆

- `sessions.db/messages`：原始对话真相。
- `memory/coremem.db` 或 `memory/akasha.db`：长期记忆引擎状态。
- `MEMORY.md`、`SELF.md`、`PENDING.md`、`RECENT_CONTEXT.md`：Markdown 档案。

删除或压缩模型上下文不能直接删除这些数据。Session 删除和长期记忆遗忘必须走各自明确操作。

## 管理命令

```bash
uv run python main.py memory doctor
uv run python main.py memory backup
uv run python main.py memory migrate
uv run python main.py memory verify
uv run python main.py memory rollback --backup-id <id>
uv run python main.py memory repair-kinds --dry-run
```

`repair-kinds` 把旧的 `identity/fact/requested_memory` 归一为可注入的 `profile`。正式执行前会备份；
先用 `--dry-run` 查看范围。

## 失败语义

| 情况 | 行为 |
| --- | --- |
| 查询 embedding 失败 | 可降级关键词 lane，并在 trace 中标明 |
| 写入 embedding 失败 | 抛错，不发布半索引记录 |
| SQLite integrity/schema 异常 | fail loud，不用旧 JSON 覆盖恢复 |
| consolidation 模型失败 | 回复保留；记录失败，不伪造记忆成功 |
| 普通 forget | 逻辑退休；物理删除只允许明确管理操作 |

内部结构见 [记忆架构](../architecture/memory.md)，评测边界见
[记忆评测](../operations/memory-evaluation.md)。

## 显式选择 Akasha2

```toml
[memory]
enabled = true
plugin = "akasha2"

[memory.embedding]
model = "your-exact-embedding-model"
base_url = "https://your-embedding-endpoint/v1"
api_key = "${EMBEDDING_API_KEY}"
output_dimensionality = 1024 # 必须填写实际维度
```

选择项是 `plugin`；`engine` 是旧结构化记忆 owner 的迁移配置，不应混用。
Akasha2 使用 `memory/akasha2.db`，v1 仍使用原配置数据库。首次启用从 cutover 后开始，
不会自动复制旧图。查询只读，事实从原始消息回源；不提供未实现的 memorize/forget 工具。
这是单用户跨会话图，不能把它当多租户隔离服务。

已有原文和固定消息向量时，可以离线生成候选：

```bash
uv run python -m scripts.replay_akasha2 \
  --source /absolute/workspace/sessions.db \
  --output /absolute/workspace/memory/akasha2-candidate.db \
  --model exact-existing-model \
  --embedding-endpoint https://your-existing-embedding-endpoint/v1
```

endpoint 只用于固定空间身份，不会发请求。缺失向量、维度混杂、内容不匹配或候选路径已存在会失败。
检查候选后，停止 runtime，备份已有 `memory/akasha2.db`，再把候选放到该路径并显式切换 plugin。
不能在 writer 运行时替换数据库。来源 stream 和原文必须保持一致。

## 工具提供者

记忆工具由 `plugins/memory_tools` 注册，完整对话工具由 `bootstrap/tools.py` 组合；
引擎的 profile 决定模型可见工具，读写风险保留在注册元数据中。
Drift 只获得检索与原文工具，不获得写入、强化、遗忘或归档。
