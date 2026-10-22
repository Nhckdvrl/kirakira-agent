<p align="center">
  <img src="./docs/assets/readme/kirakira-agent-icon.png" alt="Kirakira Agent 图标" width="96">
</p>

<h1 align="center">Kirakira Agent</h1>

<p align="center">
  <strong>有长期记忆与可组合个性的主动式个人 AI 伙伴</strong><br>
  <sub>日常陪伴 · 助理协作 · 双主动交互 · 本地优先</sub>
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

Kirakira 是一个陪伴型、助理型、主动交互型的个人 Agent，面向日常交流、兴趣、学习、生活与工作。
你可以找它随便聊聊、一起想问题，或交办事情；开启主动交互后，它也能调查新事件，
或者在空闲时回顾已经聊过的话题，决定继续帮忙、主动联系你或保持安静。

相处方式可以自己组合：温暖也可以直率，沉静也可以幽默，先听你说或顺带给个办法也能分别选择。
聊天、办事后的回答和已获准的主动联系沿用同一套设定；当前任务与明确要求优先。
长期记忆保留原始出处，工具操作、任务状态与投递结果留在同一个可恢复的 Runtime 中。

已经实测的体验包括：围绕画画、读书、吉他和薄荷的话题，在用户允许后空闲接续，结束后停止；
也包括找回过去更正过的安排或个人条件，用于新事件的调查与提醒。
场景与原始记录见[验证结果](docs/operations/verification.md)。

## Kirakira 重点打磨的体验

| 体验 | 怎样体现 | 证据 |
| --- | --- | --- |
| 按你喜欢的方式相处 | 人物底稿、六个表达维度、支持与建议偏好、说话示例分别组合；不把性格变化当成事实变化 | [同题表达、回应行为与真实任务实测](docs/operations/evaluations/2026-10-22-companion-personality.md) |
| 记得过去，还能继续核对 | 召回不足时沿出处继续查原文，让过去确认或更正的条件参与今天的回答与行动 | [长原文困难题：有依据的正确回答 0/8 → 5/8](docs/operations/evaluations/2026-10-22-memory-scenarios.md) |
| 会主动接续，也能停下 | 外部事件调查与空闲主动两条路径，可以分享或跳过；有独立开关、预算、去重和暂停 | [四类日常话题与真实等待、停止验收](docs/operations/evaluations/2026-10-22-companion-personality.md) |
| 聊天之外能认真办事 | 实际使用文件、Shell、MCP 与后台任务，核对工具结果和交付；支持中断、取消与恢复 | [真实模型主链与恢复记录](docs/operations/verification.md) |

量化结果按具体场景和版本报告。长原文对照允许追加材料与模型调用；行为实测与长期用户体验分别验证。
设计取舍与开源参考见[产品定位](docs/architecture/design-direction.md)和[人格与言行一致](docs/architecture/personality.md)。

## 为长期相处与工作准备的能力

**个性与相处。** 人物底稿保留稳定气质与立场，表达和回应方式可以独立调整。临时要求不冒充永久偏好，
说话示例不成为共同经历。使用方法见[个性与相处方式](docs/handbook/personality.md)。

**对话与工具。** 流式回复、ReAct 工具执行、文件操作、Shell 与终端会话、托管进程、MCP 和
子 Agent 使用统一的任务生命周期；会话支持插话、中断、取消和恢复。

**可追溯的记忆。** Akasha 从已完成的对话段学习。向量和图状态由消息历史派生，可以重新构建；
召回结果能够追溯到原文。本地向量提供方在显式下载模型后使用 CPU 编码。

**主动交互。** Content、Alert、Context 三类事件进入兴趣判断与唤醒流程，串联调查、分享、
去重和投递确认；可选的空闲规划接续聊天、协助事项或研究发现。本地集成可以通过已认证 RPC 提交事件。

**插件化运行时。** 模型、工具、记忆、渠道和 Web 工作台都是插件。各插件管理自己的配置和状态，
支持在运行中的组合内更新已选插件。聊天与向量模型独立配置，消息和执行状态保存在本地 workspace。

## 快速开始

使用 Python 3.14.6、[uv](https://docs.astral.sh/uv/)，Web 工作台另需 Node.js/npm。
所有 Python 命令使用项目内唯一的 `.venv` 和固定的 `uv.lock`。在仓库根目录执行：

```bash
uv sync --frozen --group dev --group eval --extra providers
npm ci
npm run build

uv run kirakira-agent init --workspace /absolute/path/to/workspace --profile web
uv run kirakira-agent gateway --config /absolute/path/to/workspace/config.toml --workspace /absolute/path/to/workspace
```

将示例路径替换为自己的目录。工作区使用本机磁盘，在 Web 工作台连接自己选择的模型。凭据保存在私有配置中。无界面运行时选择 `--profile headless`，可以跳过前端构建。

启用本地记忆、配置主动投递目标，见[记忆与主动交互指南](docs/handbook/memory-proactive.md)。
既有工作区分别按[升级说明](docs/handbook/memory-proactive.md)或[旧数据迁移指南](docs/handbook/start.md)处理。

## 验证

```bash
uv run pytest -q
uv run kirakira-verify-online --model-config /private/path/model.json --key-file /private/path/model.key --workspace /tmp/new-eval --output /tmp/native-eval.json
```

在线验证在隔离工作区调用配置的真实模型，覆盖消息、文件、Shell、MCP、托管进程、子 Agent、
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
[个性与相处](docs/handbook/personality.md) ·
[架构总览](docs/architecture/overview.md) ·
[验证记录](docs/operations/verification.md) ·
[SDK](sdk/python/README.md)

## License

MIT，见 [LICENSE](LICENSE)。
