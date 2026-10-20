# 启动和迁移

## 环境

使用项目唯一 `.venv` 和 `.python-version` 中的 Python 3.14.6：

```sh
uv sync --frozen --group dev --extra providers
```

## 新工作区

```sh
uv run kirakira-agent init --workspace /absolute/path/to/new-workspace --profile headless
uv run kirakira-agent configure-step-plan --workspace /absolute/path/to/new-workspace --key-file /private/path/step-plan.key
uv run kirakira-agent gateway --config /absolute/path/to/new-workspace/config.toml --workspace /absolute/path/to/new-workspace
```

`--profile web` 加入原生工作台组合。构建界面使用 `npm ci` 和 `npm run build`。
Core 配置只有 `[runtime].workspace`；业务配置由各插件在 `plugin-data/` 中保存。
插件制品和缓存默认位于 `~/.kirakira/plugins`，可用 `KIRAKIRA_PLUGIN_HOME` 指定。
底层原生合同仍使用 `AKASHIC_*` 环境变量。

Step5 使用 `https://api.stepfun.com/step_plan/v1`、`step-5-preview`。
密钥从私有文件导入原生 Models 的凭据存储，不写入源码或评测报告。

## 既有 Kirakira 数据

旧配置和数据库格式与当前原生格式不同。导入生成独立的新工作区：

```sh
uv run python -m scripts.migrate_legacy_workspace --source /old/workspace --destination /new/workspace
```

原工作区保持原样。新目录 `legacy/sessions.db` 是包含已提交 WAL 内容的完整快照，
旧配置备份只允许当前用户读取。消息 ID、顺序、时间、正文和工具引用保留。
导入消息使用 `legacy:` 来源，作为可读取历史，不自动重放旧程序或恢复旧工具授权。
记忆图、未完成的外部效果和旧任务状态保留在原工作区或快照中，不能当作当前原生状态直接启用。
旧项目归属和目录默认值也不自动转换为新会话的固定属性。
随后使用 `configure-step-plan` 配置新 Models。

## 验证

```sh
uv run python -m scripts.check_upstream_kernel
uv run pytest -q
uv run kirakira-verify-online --key-file /private/path/step-plan.key --workspace /tmp/new-native-eval --output /tmp/native-eval.json
```

在线验证使用真实生产插件图和真实 Step5，MCP/进程测试设施在隔离目录内运行。
