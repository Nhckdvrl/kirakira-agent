# Kirakira Agent

具备主动交互和长期记忆的多渠道 Agent Runtime。

当前运行主干移植自 [Akashic Agent](https://github.com/kachofugetsu09/akashic-agent) 的
`e1628ddc8a33c2840e5b4558ff3b66af207eec2a`，采用完整原生插件与执行架构。
来源、文件摘要及许可证见 [UPSTREAM.json](UPSTREAM.json)、[THIRD_PARTY.md](THIRD_PARTY.md)。

消息接纳、来源任务、回复程序、模型、工具、记忆、主动交互、投递和界面由独立插件拥有。
同一 CompositionRoot 负责 Context/Fiber/Effect 生命周期；Task 管理接纳、撤权与物理排空。
Ledger 提供唯一消息事实、窄读写权限、同库 owner 事务与固定 provider bindings。
工具执行使用 prepared 目标，支持本地进程、HostBridge、MCP、托管进程和可选 Workload controller。

## 开始使用

项目只使用 Python 3.14.6 和一个 `.venv`：

```sh
uv sync --frozen --group dev --extra providers
uv run kirakira-agent init --workspace /absolute/path/to/workspace --profile headless
uv run kirakira-agent configure-step-plan --workspace /absolute/path/to/workspace --key-file /private/path/step-plan.key
uv run kirakira-agent gateway --config /absolute/path/to/workspace/config.toml --workspace /absolute/path/to/workspace
```

Step5 驱动固定使用 Step Token Plan 专用地址，不跟随重定向或回退其他计费路由。
主配置只保留 `[runtime].workspace`，业务设置归各插件的数据目录。
使用 `--profile web` 加入工作台，并运行 `npm ci && npm run build` 构建界面。

旧 Kirakira 工作区需要显式导入新目录，原目录保留：

```sh
uv run python -m scripts.migrate_legacy_workspace --source /old/workspace --destination /new/workspace
```

导入保留原始消息、时间和工具引用，旧程序和授权不自动重放；记忆图仍保留在原工作区。
具体设置和迁移范围见[启动指南](docs/handbook/start.md)。

## 验证

```sh
uv run python -m scripts.check_upstream_kernel
uv run pytest -q
uv run kirakira-verify-online --key-file /private/path/step-plan.key --workspace /tmp/new-eval --output /tmp/native-eval.json
```

在线评测使用实际生产插件图和真实 Step5，覆盖消息、文件、Shell、MCP、托管进程、子代理、
局部更新、插话、暂停恢复、来源抢占、附件和重启。
可选外部提供方需要单独配置；验证范围见[当前状态](docs/status/current.md)和[验证记录](docs/operations/verification.md)。

[架构](docs/architecture/overview.md) · [文档目录](docs/INDEX.md) · [SDK](sdk/)
