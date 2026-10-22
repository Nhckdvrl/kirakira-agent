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
你可以找它一起想问题，分享一点新发现，或者交办事情。开启主动交互后，它也能调查收到的新信息，
或在空闲后接续用户愿意继续的话题。

我们重点打磨持续相处的体验：熟悉的交流方式、能够回头核对的记忆，以及跨对话接续的实际帮助。
人格、记忆和主动行为与工具、任务、投递运行在同一套 Runtime 中。

## 怎样与你相处

**按你喜欢的方式交流。** 温暖、直率、幽默、追问、展开程度与表情可以独立组合；
遇到困扰时先听你说或给个小办法，闲聊中是否顺带建议，也能分别选择。
可编辑的人物底稿和可选说话示例保留个人化空间，当前明确要求与任务格式优先。
[个性与相处方式 →](docs/handbook/personality.md)

**记得过去，也能继续核对。** Akasha 从已完成的对话段学习，结合语义召回与情景关联。
召回片段不够时，可以继续定位和展开原始消息，检查更正、条件与原话；
Markdown 长期档案保存常用背景，原始消息始终保留。
[记忆怎样工作 →](docs/architecture/memory.md)

**有理由地主动找你。** 两条路径共用执行和投递链路：已配置的信息源可以触发调查，
可选的空闲规划则提出聊天接续、事项协助或研究。两者都可以选择跳过。
主动接续聊天需要单独开启；预算、去重、暂停和话题完成后的停止约束主动行为。
[记忆与主动交互配置 →](docs/handbook/memory-proactive.md)

**聊天之外能认真办事。** 流式对话、文件、Shell、MCP、子 Agent 与后台任务使用统一的任务生命周期。
工具结果、中断、取消、恢复和投递回执与原执行关联。
模型、渠道和工具可以通过插件替换，聊天模型与向量模型独立配置。
[Runtime 架构 →](docs/architecture/overview.md)

例如，一个持续关注的兴趣可以从聊天变成记忆里的个人条件，再参与后续新事件的判断。
已配置的订阅源、网页或邮箱提供事件，Agent 回看相关历史后决定分享或保持安静；
你可以检查来源和投递结果，修改关注规则，或者暂停跟进。
[持续跟进的使用路径 →](docs/handbook/followups.md)

可以直接在聊天里交办：

| 你说 | 它实际做 |
| --- | --- |
| “读这篇资料，整理成表格，保存后再核对。” | 读取网页、写入文件并读回检查 |
| “十分钟后提醒我读五页书。” | 登记定时提醒，到点投递 |
| “持续关注这个活动页，符合我之前说的条件才通知。” | 核对主动联系配置，绑定本轮原话；新变化结合历史调查 |
| “改成只有周末才提醒，之后先暂停。” | 保存新的关注规则并暂停采集，保留已有记录 |

持续跟进需要先配置记忆和通知位置；也可以在聊天中查看决定依据及实际送达情况。

## 快速开始

使用 **Python 3.14.6**、[uv](https://docs.astral.sh/uv/)；Web 工作台另需 **Node.js 24**。
所有 Python 命令使用项目唯一的 `.venv` 和固定依赖锁。
工作区请选择本机磁盘上一个全新的空目录：

```bash
git clone https://github.com/Nhckdvrl/kirakira-agent.git
cd kirakira-agent
uv sync --frozen --extra providers --extra memory
npm ci
npm run build

uv run --frozen --no-sync kirakira-agent init --workspace "$HOME/.kirakira/workspace" --profile web
uv run --frozen --no-sync kirakira-agent gateway --config "$HOME/.kirakira/workspace/config.toml" --workspace "$HOME/.kirakira/workspace"
```

打开 **http://localhost:2236**。首次启动的主人账号为 `kirakira`，
初始密码保存在工作区的私有文件 `web-auth/initial-password` 中。
登录后改成自己的密码，连接模型，即可开始对话。gateway 在前台运行，使用期间需要保持进程开启。

接着可以调整“**个性与相处**”、启用[本地记忆](docs/handbook/memory-proactive.md#本地记忆)，
或者设置[持续跟进](docs/handbook/followups.md)。空闲主动默认关闭。
终端模型配置、无界面使用、Telegram 与升级方式见[启动指南](docs/handbook/start.md)。

本地优先指消息与执行状态保存在自己的工作区；使用远程模型或外部工具时，仍会向对应服务发送完成请求所需的输入。
向量编码器在显式下载模型后使用本地 CPU 编码。

## 已经测到了什么

| 问题 | 实际结果 | 条件与完整记录 |
| --- | --- | --- |
| 沿记忆线索继续查原文是否有帮助？ | 长原文中部、末页困难题，有依据的正确回答 **0/8 → 5/8** | 初始材料相同，查原文增加材料和模型调用。[记忆场景实验](docs/operations/evaluations/2026-10-22-memory-scenarios.md) |
| 过去的个人条件能否影响新事件判断？ | 对应历史呈现 **12/12**，分享或跳过判断正确 **11/12** | 受控邮箱场景，保留首轮误判。[历史参与行动](docs/operations/evaluations/2026-10-21-mail-followups.md) |
| 能否接续日常话题，并在结束后停下？ | 四类受邀话题，**32/32 验收断言** | 实际等待空闲，检查出处、投递、去重和停止。[伙伴体验实测](docs/operations/evaluations/2026-10-22-companion-personality.md) |
| 真实模型下执行主链是否跑通？ | **13/13 主链场景** | 文件、Shell、MCP、子任务、中断与恢复。[验证记录](docs/operations/verification.md) |

这些结果属于各自的实验条件，不代表记忆全面领先或用户满意度。
个性设置已经接通并实测，实际回复仍可能夹带建议或补写无依据细节；
跨模型一致性、长期陪伴效果以及主动联系的价值和误打扰仍需继续验证。
[报告](docs/operations/verification.md)保留失败、开销和版本；[设计依据](docs/architecture/design-direction.md)解释取舍。

## 开发与验证

```bash
uv run --frozen --no-sync pytest -q
npm run typecheck
uv run --frozen --no-sync python -m scripts.check_docs
```

[评测说明](eval/README.md)提供隔离工作区的真实模型测试和记忆实验；
[维护说明](scripts/README.md)提供来源校验、构建与缓存清理命令。

```text
agent/          组合内核、生命周期、配置与 supervisor
bootstrap/      宿主资源与启动
plugins/        对话、人格、记忆、工具、主动交互与渠道
frontend/       Web 界面、主题、共享包、类型与前端配置
kirakira_agent/ 公开 CLI 与工作区设置
sdk/            程序化客户端
eval/           功能验证与记忆实验
tests/          Runtime 与集成合同
scripts/        构建、迁移与维护
docs/           使用指南、架构与评测证据
third_party/    来源、第三方声明与补丁
```

[文档](docs/INDEX.md) · [启动指南](docs/handbook/start.md) ·
[个性与相处](docs/handbook/personality.md) · [记忆与主动交互](docs/handbook/memory-proactive.md) ·
[架构](docs/architecture/overview.md) · [SDK](sdk/python/README.md) ·
[当前状态](docs/status/current.md) · [后续工作](docs/status/roadmap.md)

## License

MIT，见 [LICENSE](LICENSE)。
