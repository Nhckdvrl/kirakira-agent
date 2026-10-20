# Workspace 迁移

`migrations/yoyo/` 是唯一自动执行的 workspace migration catalog。启动时先获取 workspace 单实例锁，
再应用未执行迁移并写入 `<workspace>/migrations.sqlite3`，之后才打开运行状态库。

## 规则

1. 已发布的 migration 文件不可修改或删除。
2. 新 migration 必须用 `__depends__` 明确依赖关系。
3. migration 应可重复检查，失败时阻止 runtime 继续打开状态。
4. `.instance.lock` 的作用域是一个 workspace，不是整个仓库或用户目录。
5. schema 演进归 migration 管，启动代码不应散落临时 ALTER 语句。

origin migration 只记录 Kirakira 当前 workspace schema，不会改写 `sessions.db`、`memory/coremem.db`、
`memory/akasha.db` 或 Markdown memory 内容。这些数据层有各自的 owner 和兼容策略。

`20260815_01_session_context_compaction` 会先在 `.kirakira/backups/` 生成 SQLite 备份，再为 SessionDB 添加独立 compaction cursor 和只追加 ledger。它不修改 `messages` 正文。

## 验证

```bash
uv run pytest -q tests/test_migration_runner.py tests/test_yoyo_migration_append_only.py tests/test_workspace.py
```

这些测试覆盖 migration runner、append-only 目录合同和 workspace 初始化/锁边界。

## 2026-10-02：消息发布位置

新增 `20261002_01_message_publications`，依赖 context compaction migration；历史迁移文件未修改。
仅增加 source stream 身份、assistant publication 表及 INSERT trigger，不补写既有历史，也不复制正文。
新 SessionManager 启动同样确保这份 schema，因此直接构造 SessionManager 的测试/CLI 路径行为一致。

Akasha sidecar 首次激活记录 cutover，之后 source cursor 与图同事务推进。恢复备份时需要匹配的 source/graph，
源流身份改变或源 head 回退会明确失败。具体行为见[ADR0007](../decisions/0007-durable-consumption-and-proactive-supersession.md)。
