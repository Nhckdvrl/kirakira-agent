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

## 运行参数

Web 工作台的插件设置中可以调整以下参数；每个设置由对应插件管理，保存后采用新的配置版本。

| 设置 | 可调整的内容 |
| --- | --- |
| 长期情景记忆 | 每轮召回条数、注入字数预算；不在界面修改学习动力学 |
| 长期记忆文档 | 档案篇幅、相关主题浮现、整理开关与间隔 |
| 对话回复、子任务 | 步数、输出预算、子任务并发和交回结果长度 |
| 定时任务、后台通知 | 活动任务上限、执行预算、完成后唤醒或留待下次对话 |
| 上下文压缩、会话标题 | 保留最近原文的预算、标题长度 |
| 命令执行 | 默认超时、工具返回长度 |
| Web 工具、网页读取 | 提供方、结果数、超时、响应与正文上限、语言和代理 |
| 空闲思考 | 参考对话、思考范围、空闲等待、规划间隔和每日预算；默认关闭 |

数字字段留空并保存会删除该字段的显式覆盖，恢复界面显示的默认值；其他已设置字段保留。越界值在保存前校验，配置切换通过原生回执确认。已经开始的任务仍使用其固定的程序和工具绑定，新配置用于之后的新执行。

记忆召回预算控制传给模型的材料量，不应据此推断记忆更准确。已有学习图和原始消息不会因修改召回预算而删除；修改向量空间或学习动力学仍需要遵循[记忆配置与重建流程](memory-proactive.md)。

文件读取同时受分页参数、400 行和 10KB 呈现上限约束；大文件可先小范围预览，再按行定位，行数统计仍会扫描全文。网页的响应字节上限与返回文本字符上限分别生效，二者都不是模型 token 配额。命令输出截断时可按结果中的 `output_path` 读取完整输出。具体边界和验收见[工具报告](../operations/evaluations/2026-10-21-tool-runtime.md)。

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
每次迁移的实际地址映射和快照摘要以目标工作区中的 `migration.json` 与私有 `legacy/` 为准，不使用仓库中的历史测试数字代替数据核验。

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
