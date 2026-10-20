<p align="center">
  <img src="./docs/assets/readme/kirakira-agent-icon.png" alt="Kirakira Agent 图标" width="96">
</p>

<h1 align="center">Kirakira Agent</h1>

<p align="center">
  <strong>本地优先、可持续运行的多渠道 AI Agent Runtime</strong><br>
  <sub>被动对话 · 主动执行 · 记忆 · 插件 · MCP</sub>
</p>

<p align="center">
  <a href="./README-cn.md"><b>简体中文</b></a> ·
  <a href="./README.md">English</a> ·
  <a href="./docs/INDEX.md">文档</a>
</p>

<p align="center">
  <a href="https://github.com/Nhckdvrl/kirakira-agent/stargazers"><img src="https://img.shields.io/github/stars/Nhckdvrl/kirakira-agent?style=flat&color=f29ab2" alt="GitHub Stars"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/github/license/Nhckdvrl/kirakira-agent?color=738bd7" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.14.6-64a8df" alt="Python 3.14.6">
  <img src="https://img.shields.io/badge/runtime-local--first-f2b84b" alt="Local-first runtime">
</p>

<p align="center">
  <img src="./docs/assets/readme/kirakira-agent-hero.png" alt="Kirakira Agent：空舞台、乐器、星光与三条运行链" width="100%">
</p>

## Kirakira 是什么

Kirakira 是一个本地优先、多渠道的 AI Agent Runtime。它把对话、主动交互、长期记忆和工具执行
放在同一个可扩展的运行时中；会话和运行状态默认保存在本地 workspace。

事件记忆以完整原始对话为事实源，将语义召回与图式联想补全结合，恢复跨会话相关经历。
模型可以沿来源引用展开原文，核对历史上下文；Markdown 记忆则保存明确的档案、偏好和笔记。

主动交互从事件和背景信息出发：Agent 可以调查新信息，判断是否值得发起交互，再投递到指定会话。
日常对话与主动任务共用模型、工具和记忆。

## 一分钟了解

| 你关心的问题 | Kirakira 的做法 |
| --- | --- |
| Agent 如何工作 | 被动对话与主动任务共用可持续运行的 Runtime |
| 数据放在哪里 | 消息、记忆、配置和执行状态保存在选定的本地 workspace |
| 如何操作不同仓库 | 项目和会话工作目录将仓库上下文带入文件工具、Shell 与子 Agent |
| 如何记住过去 | 语义召回、联想补全与 Markdown 档案协作，保留原始消息证据 |
| 如何扩展 | 通过 Plugin、MCP、Skill、Tool 和事件来源增加能力 |
| 如何控制上下文 | 上下文投影与压缩管理请求预算，完整历史仍保留在消息库中 |
| 如何排查运行过程 | 记录模型用量、工具结果、任务状态与投递回执 |
| 模型是否绑定 | 模型提供方以插件接入，内置 Step5 Token Plan 和本地向量提供方 |

## 为长期任务准备的能力

**对话与工具。** 流式回复、ReAct 工具执行、文件操作、Shell 与终端会话、托管进程、MCP 和
子 Agent 使用统一的任务生命周期；会话支持插话、中断、取消和恢复。

**可追溯的记忆。** Akasha 从已完成的对话段学习。向量和图状态由消息历史派生，可以重新构建；
召回结果能够追溯到原文。本地向量提供方在显式下载模型后使用 CPU 编码。

**主动交互。** Content、Alert、Context 三类事件进入兴趣判断与唤醒流程，串联调查、分享、
去重和投递确认。本地集成可以通过已认证 RPC 提交事件。

**插件化运行时。** 模型、工具、记忆、渠道和 Web 工作台都是插件。各插件管理自己的配置和状态，
支持在运行中的组合内更新已选插件。

## 快速开始

使用 Python 3.14.6、[uv](https://docs.astral.sh/uv/)，Web 工作台另需 Node.js/npm。
所有 Python 命令使用项目内唯一的 `.venv` 和固定的 `uv.lock`。在仓库根目录执行：

```bash
uv sync --frozen --group dev --group eval --extra providers
npm ci
npm run build

uv run kirakira-agent init --workspace /absolute/path/to/workspace --profile web
uv run kirakira-agent configure-step-plan --workspace /absolute/path/to/workspace --key-file /private/path/step-plan.key
uv run kirakira-agent gateway --config /absolute/path/to/workspace/config.toml --workspace /absolute/path/to/workspace
```

将示例路径替换为自己的目录。工作区使用本机磁盘，API key 放在私有文件中；Step5 驱动使用
Step Token Plan 专用端点。无界面运行时选择 `--profile headless`，可以跳过前端构建。

启用本地记忆、配置主动投递目标，见[记忆与主动交互指南](docs/handbook/memory-proactive.md)。
既有工作区分别按[升级说明](docs/handbook/memory-proactive.md)或[旧数据迁移指南](docs/handbook/start.md)处理。

## 验证

```bash
uv run pytest -q
uv run kirakira-verify-online --key-file /private/path/step-plan.key --workspace /tmp/new-eval --output /tmp/native-eval.json
```

在线验证在隔离工作区调用真实 Step5，覆盖消息、文件、Shell、MCP、托管进程、子 Agent、
插话、重启和插件更新。记忆与主动交互实验的量化结果、运行条件见[验证记录](docs/operations/verification.md)。

## 目录

```text
agent/            组合内核、生命周期、配置与 supervisor
bootstrap/        宿主资源与应用启动
plugins/          对话、模型、工具、记忆、主动交互与渠道
frontend/         Web 公共界面与主题
kirakira_agent/   公开 CLI、工作区设置与发行版集成
sdk/              程序化客户端
eval/             功能验证与记忆实验
tests/            Runtime 与集成合同
scripts/          构建、迁移与验证工具
docs/             架构、使用指南与评测记录
```

## 文档

[文档索引](docs/INDEX.md) ·
[启动指南](docs/handbook/start.md) ·
[记忆与主动交互](docs/handbook/memory-proactive.md) ·
[架构总览](docs/architecture/overview.md) ·
[验证记录](docs/operations/verification.md) ·
[SDK](sdk/python/README.md)

## License

MIT，见 [LICENSE](LICENSE)。
