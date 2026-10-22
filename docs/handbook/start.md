# 启动与日常使用

## 环境与安装

在仓库根目录操作，使用 `.python-version` 固定的 Python 3.14.6 和项目唯一的 `.venv`。
Web 构建使用 Node.js 24。不要另建一套 Python 环境：

```sh
uv sync --frozen --extra providers --extra memory
npm ci
npm run build
```

`providers` 安装可选提供方与 Web 工作台依赖，`memory` 安装本地 CPU 向量编码器。
安装依赖不会自动下载向量权重或启用主动联系。开发依赖随默认 `dev` 组安装；运行环境可增加 `--no-dev`。
下面的 `--no-sync` 使用已同步的环境，避免运行命令再次改变依赖选择；调整依赖后先重新执行 `uv sync`。

## 创建 Web 工作区

工作区放在仓库外的本机磁盘，选择全新的空目录；数据库不建议放 NFS：

```sh
uv run --frozen --no-sync kirakira-agent init --workspace "$HOME/.kirakira/workspace" --profile web
uv run --frozen --no-sync kirakira-agent gateway --config "$HOME/.kirakira/workspace/config.toml" --workspace "$HOME/.kirakira/workspace"
```

打开 http://localhost:2236。首次启动的主人账号为 `kirakira`，初始密码保存在工作区
`web-auth/initial-password`，登录后需要修改。已有账号的用户名与密码在升级时保留。
工作台中连接聊天模型后开始对话；模型必须支持所需的工具调用能力。

gateway 在前台运行，`Ctrl+C` 正常停止；终端关闭后不会自动常驻。
无命令启动或 `supervise` 使用 Runtime 的 supervisor，长期部署还需由系统服务管理进程。
默认只监听本机；远程访问应通过自己的隧道或代理连接。

首次使用可以依次做三件事：在“个性与相处”中调整交流习惯，按[记忆说明](memory-proactive.md#本地记忆)
准备向量资产，然后选择是否启用[跟进](followups.md)或[空闲思考](memory-proactive.md#空闲思考)。
人物底稿在工作区 `memory/VEDA.md`，组合设置不会覆盖该文件。

## 终端配置模型与无界面使用

无界面工作区选择 `--profile headless`，可以跳过 Node.js 安装与 Web 构建。
以下示例的目录、模型、API 地址与密钥文件需换成实际值；配置前停止该工作区实例：

```sh
uv run --frozen --no-sync kirakira-agent init --workspace /local-disk/headless --profile headless
uv run --frozen --no-sync kirakira-agent configure-model \
  --workspace /local-disk/headless --model YOUR_MODEL --base-url YOUR_API_BASE \
  --key-file /private/path/model.key
uv run --frozen --no-sync kirakira-agent gateway \
  --config /local-disk/headless/config.toml --workspace /local-disk/headless
```

`--key-file` 读取只含 API key 的非空私有文件，凭据交由 Models 和凭据存储管理。
`--driver` 默认为 OpenAI 兼容驱动；订阅账户必须使用相应订阅 API 地址与提供方，不能改成现金端点。
聊天供应商与本地记忆无绑定关系。

同一个本地 `--id` 表示同一模型身份；换用另一模型时指定新 `--id`，成功后该模型成为默认。
既有连接、模型能力与凭据的编辑也可以在工作台完成。
上下文和输出预算按实际模型填写，必须满足 `0 < max-output-tokens < context-window`。
完整配置参数可用 `kirakira-agent configure-model --help` 查看。

配置文件只包含 `[runtime].workspace`；业务配置由插件在 `plugin-data/` 中保存。
插件制品和缓存默认位于 `~/.kirakira/plugins`，可用 `KIRAKIRA_PLUGIN_HOME` 指定。
消息和执行状态保存在本地；远程模型与外部工具会接收请求所需的输入。

## Telegram

Web 工作区在组件管理中选中 Telegram 消息接入与消息推送，再到各自设置填写 Bot Token。
接入负责收信和原对话回复，推送提供主动提醒与通知；同一个机器人通常需要配置两者。
允许用户列表填写用户名且不含 `@`，留空允许所有用户。

先在 Telegram 中主动与机器人开始对话。一个 Token 同时只保留一个收信实例；
已有常驻实例时，不要再运行另一套轮询或手动调用 `getUpdates`。
主动投递的位置还需在对应主动功能中明确设置，启用收信不等于开启空闲主动。
流式正文、最终投递与模型等待的区别见[链路测量](../operations/evaluations/2026-10-22-telegram-latency.md)。

## 运行参数

参数由相应插件保存，配置换代生效：

| 设置 | 可调整的内容 |
| --- | --- |
| 个性与相处 | 六个表达维度、支持与建议习惯、补充偏好和说话示例 |
| 长期情景记忆 | 每轮召回条数、注入字数预算 |
| 长期记忆文档 | 档案篇幅、主题浮现、整理开关与间隔 |
| 对话回复、子任务 | 步数、输出预算、并发和交回长度 |
| 定时任务、后台通知 | 活动上限、执行预算、完成后唤醒或留待下次对话 |
| 上下文压缩、会话标题 | 保留原文预算、标题长度 |
| 命令执行 | 默认超时、工具返回长度 |
| Web 工具、网页读取 | 提供方、结果数、超时、响应与正文上限、语言和代理 |
| 空闲思考 | 参考对话、范围、等待、间隔和每日预算；默认关闭 |

数字字段留空并保存会删除该字段的覆盖，恢复默认；其他设置保留。
已开始的任务仍使用固定的程序和工具绑定，新配置用于后续执行。
召回预算控制材料量，不等于记忆准确性；改预算不会删除历史或重学整张图。
改变向量空间或学习动力学需遵循[记忆配置与重建](memory-proactive.md)。

文件读取受分页、400 行和 10KB 呈现上限约束；命令截断输出可按 `output_path` 继续读取。
网页响应字节上限与返回文本字符上限分别生效。边界与验收见[工具说明](../operations/evaluations/2026-10-21-tool-runtime.md)。

## 既有 Kirakira 数据

当前格式的工作区停止实例后按[升级说明](memory-proactive.md#升级既有工作区)更新。
早期配置和数据库必须显式导入独立的新工作区，不能直接升级或覆盖原数据库：

```sh
uv run --frozen --no-sync python -m scripts.migrate_legacy_workspace \
  --source /old/workspace --destination /local-disk/new-workspace --profile web
```

迁移器取得旧实例锁，通过 SQLite backup 读取已提交快照，合并数据库与 JSON 历史，
保留原始 ID、顺序、时间、工具引用与附加信息。人格、档案、Skill 和上传文件按现有转换规则处理；
不把旧执行授权或未完成操作恢复为新授权。冲突及未支持的非空状态会拒绝发布。
目标中的 `migration.json` 记录映射和摘要，私有 `legacy/` 保留迁移来源。

启动新工作区并核对历史后再切换服务入口。需要真实模型验证与原子入口切换时，
使用迁移器的 `--model-config`、`--key-file` 和 `--activate-config`，具体参数见该命令 `--help`；
模型 JSON 格式见[评测配置](../../eval/README.md#模型条件)。失败保留原入口，原始数据不覆盖。
旧实现和一次性切换记录通过 Git 查看，不作为当前启动步骤。

## 验证与清理

```sh
uv run --frozen --no-sync pytest -q
uv run --frozen --no-sync python -m scripts.check_docs
uv run --frozen --no-sync python -m scripts.clean_workspace --dry-run
```

真实模型测试与记忆量化实验见[评测说明](../../eval/README.md)，维护入口见[脚本说明](../../scripts/README.md)。
测试在新的隔离工作区运行，原始日志可能包含初始登录凭据，不上传整个工作区。
