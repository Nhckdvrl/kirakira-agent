# 启动和迁移

## 环境

使用项目唯一 `.venv` 和 `.python-version` 中的 Python 3.14.6：

```sh
uv sync --frozen --group dev --extra providers
```

## 新工作区

```sh
uv run kirakira-agent init --workspace /absolute/path/to/new-workspace --profile headless
uv run kirakira-agent configure-model --workspace /absolute/path/to/new-workspace --model YOUR_MODEL --base-url YOUR_API_BASE --key-file /private/path/model.key
uv run kirakira-agent gateway --config /absolute/path/to/new-workspace/config.toml --workspace /absolute/path/to/new-workspace
```

`--profile web` 加入工作台组合。构建界面使用 `npm ci` 和 `npm run build`。
Core 配置只有 `[runtime].workspace`；业务配置由各插件在 `plugin-data/` 中保存。
插件制品和缓存默认位于 `~/.kirakira/plugins`，可用 `KIRAKIRA_PLUGIN_HOME` 指定。
底层兼容协议由入口封装，业务配置不依赖供应商名称。

聊天模型经统一 Models 接口配置；也可以在 Web 工作台连接其他提供方。
密钥从私有文件导入凭据存储，不写入源码或评测报告。
订阅端点使用对应 driver；例如订阅计划可通过 `configure-step-plan` 单独配置。

## 既有 Kirakira 数据

旧配置和数据库格式与当前格式不同。导入生成独立的新工作区：

```sh
uv run python -m scripts.migrate_legacy_workspace --source /old/workspace --destination /new/workspace
```

完整迁移并验证、切换实际入口：

`model.json` 明确指定 `model`、`endpoint`、`driver_id` 与上下文预算，格式见[评测配置](../../eval/README.md#模型条件)。密钥保存在单独的私有文件中。

```sh
uv run python -m scripts.migrate_legacy_workspace \
  --source /old/workspace --destination /local-disk/new-workspace --profile web \
  --model-config /private/path/model.json --key-file /private/path/model.key --activate-config /old/workspace/config.toml
```

工作区数据库应位于本机磁盘；源码和项目目录可以位于共享 NFS。
迁移期间取得原 Runtime 的实例锁；读取 SQLite 备份 API 生成的已提交快照，
合并 SQL、JSON 会话和旧索引，发现同一消息位置内容冲突时中止。
原始 ID、顺序、时间、正文、工具引用和 JSON 附加信息保留。
旧地址转换为 `akashic:legacy:<原地址>`，工作台可直接打开并继续对话；
`migration.json` 保存完整地址映射。 `akashic:` 地址保持不变。

旧长期记忆条目转换为 MEMORY.md，原始 Markdown 文档进入 topics/；
人格进入 VEDA.md，本地 Skill 与 uploads 一并复制，既有会话的工作目录固定为原工作区。
原始数据、配置、空向量库和索引另存于私有 `legacy/`，逐文件保存摘要。
已有的旧备份目录继续保留，不重复递归复制。

纯旧对话采用 `legacy-unattributed` 来源并带出处证明；
旧类型化消息采用 `legacy:<原来源>`，都不会重新取得执行授权。
记忆插件可以读取导入来源。旧参数转换到 Reply，聊天模型按显式配置接入。
旧外部渠道的停用状态保持停用，不产生外发消息。

迁移器遇到非空旧向量图、owner 状态、项目 scope、主动任务或 MCP/插件声明时会拒绝发布，
要求补充对应的显式转换，不能把数据仅存档后宣称迁移成功。
历史迁移的具体数据与恢复点见[迁移记录](../operations/evaluations/2026-10-20-workspace-migration.md)。

只有启动、全部历史读回及真实模型回复通过，且全部源文件与快照仍一致时，
才原子替换 `--activate-config`。验证失败时入口保持原配置，新工作区和备份保留供排查。
回退旧实现时先停止新实例，使用迁移前提交 `65c8181`，再恢复 `legacy/config.toml`。
旧格式需配合旧实现读取；原始数据库没有被覆盖。快照可恢复到本机数据盘后继续使用当前实现。

## 验证

```sh
uv run python -m scripts.check_upstream_kernel
uv run pytest -q
uv run kirakira-verify-online --model-config /private/path/model.json --key-file /private/path/model.key --workspace /tmp/new-native-eval --output /tmp/native-eval.json
```

在线验证使用生产插件图和配置的真实模型，MCP/进程测试设施在隔离目录内运行。
