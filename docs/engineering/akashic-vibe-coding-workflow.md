# Akashic Vibe Coding 工作流

版本日期：2026-10-07。依据 Akashic 主分支 `50b93c7e778cfc301fbd75f9df9ec1a3041447ed`。

这份手册描述上述固定版本的 Akashic 开发流程，可用于指导维护者与 Coding Agent 协作。维护者确定产品意图和关键设计，Agent 在明确授权与项目约束内自主完成实现，并提供能够检查实际结果的证据。

整个流程按 **理解边界 → 实现并验证 → 交付评审** 推进。[工作流原文](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/WORKFLOW.md)。

## 共享工作手册

项目需要一套进入版本控制的共享工作手册，让每次新会话都能找到相同的需求、决定和接手位置。

| 文件 | 保存的内容 | 维护方式 |
| --- | --- | --- |
| `docs/INDEX.md` | 阅读入口与任务路由 | 文件移动、增删时更新相关入口 |
| `docs/projectneed.md` | 长期需求、数据规则、权限和行为约束 | 使用稳定条款 ID；语义变化先确认 |
| `docs/WORKFLOW.md` | 开工、实现、验证和交付的方法 | 保存通用执行顺序 |
| `docs/NOW.md` | 尚未完成的工作、阻塞和接手限制 | 完成即删除；历史由 Git、PR 保存 |
| `docs/decisions/` | 重要设计选择、理由和适用条件 | 标明状态、替代关系和重议条件 |
| `docs/design/` | 具体问题的架构、迁移、失败处理与验收 | 从实际调用链和状态变化出发 |
| `docs/writing-rules.md` | 文档归属与维护规则 | 保持事实位置清晰 |
| `docs/templates/` | 复杂任务、评审与交接模板 | 按需要使用 |

同一个事实只设一个权威位置，其他文档通过链接或条款 ID 引用。Akashic 的根目录 `AGENTS.md`、`CLAUDE.md` 属于本地运行环境指令；项目协作规则保存在上述版本化工作手册中。[文档维护规则](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/writing-rules.md)。

## 第一步 理解本次任务的边界

### 阅读当前依据

每个新会话先读 `docs/INDEX.md`，再读项目设计原则。修改任务继续读 `WORKFLOW.md`；非简单任务读取 `NOW.md` 中与本次工作有关的事项。

随后按本次涉及的行为、数据对象和负责模块展开阅读：

- 找到相关需求条款和有效决策。
- 跟随决策的勘误、替代关系，确认当前适用范围。
- 追踪真实入口、调用链、状态读写和结果消费者。
- 跨多个模块的任务，展开相关交互边界。
- 涉及持久数据时，继续读取持久化状态地图。

例如，“缩小模型窗口”需要一起检查上下文投影和历史保留规则；“删除无用源码”需要核对动态加载、插件消费者和持久数据的关系。阅读范围由实际影响决定。[阅读索引](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/INDEX.md)。

### 说明目标与完成标准

在开工前，把需求转成可以判断是否完成的结果。普通任务用一段短说明即可。涉及持久数据、职责归属、生命周期、公共接口或外部效果时，说明四件事：

1. 本次改变什么。
2. 哪些行为和状态必须保持。
3. 依据哪项需求或有效决定。
4. 从什么入口验证，观察什么结果。

可以这样写：

> 本次降低长会话的模型输入开销，调整发送给模型的历史窗口。既有消息正文、身份和顺序保持，上下文模块继续通过只读接口获取历史。依据非破坏性投影的决定，通过实际请求观察输入变化，再关闭并重开会话检查完整历史，并确认后续消息序号连续。

完成标准要来自用户需求和已确认的设计。实现过程中发现困难，应提出调整方案；不能修改预期结果，让当前实现看起来已经完成任务。

复杂任务需要展开时，补充真实调用链、允许的状态变化、失败与取消处理、迁移和恢复方式。开工材料以够用为准。[任务说明模板](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/templates/agent-task-contract.md)。

### 明确自主执行范围

执行范围也要明确。读取材料、修改已授权的本地代码、执行非破坏性验证，可以自主推进。需要改变数据保留、权限、公共行为或现行决定，而现有授权不足时，先说明理由、影响和选择，由维护者决定。

不熟悉的领域，先补齐影响选择的关键事实。Agent 可以查官方文档和成熟实现，说明适用条件与代价，再把方案放回当前项目的接口、状态和生命周期中判断。

## 第二步 在隔离环境中实现并验证实际结果

### 准备源码与环境

非简单修改从最新目标分支建立独立 worktree，记录基线，保护原 checkout 的未提交工作。下面是一个任务分支示例：

```bash
git fetch origin main
git worktree add -b feature/my-task ../my-task origin/main
cd ../my-task
git rev-parse HEAD
```

每个 worktree 同时只有一个 writer。使用多个 Agent 时，为需要写代码的执行者分别分配 worktree 和分支，明确负责范围；只读评审者可以读取完整 diff 和相关材料。

需要 CodeGraph 时，在当前 worktree 独立初始化并检查索引路径。Python 环境也要先核对：依赖一致时可以复用原虚拟环境；依赖发生变化时使用独立环境。纯文档任务只准备必要的文档检查环境。

### 实现完整职责

实现时，围绕本次目标追踪完整职责：

- 输入怎样被接纳和校验。
- 哪个模块拥有状态，谁可以读，谁可以写。
- 并发、取消、失败和重启时发生什么。
- 哪个时刻提交持久事实或外部效果。
- 下游如何读取结果，资源何时关闭。

接口应只提供调用方完成任务所需的能力。只读计算使用只读材料，元数据修改使用受限 writer，破坏性操作由明确的数据管理入口负责。

插件改动还要检查实际装配、依赖、安装和生命周期。静态依赖检查可以发现部分越界，但能力是否可加载、可替换和正确收尾，需要相应的运行证据。[插件边界决定](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/decisions/0065-plugin-boundary-checks-do-not-grant-core-ownership.md)。

### 处理持久状态与恢复

涉及状态时，先区分权威持久事实、受保护派生索引、临时运行视图和外部效果。对每类持久对象写清正常增加、允许更新、逻辑失效、物理删除条件及负责模块。

例如，模型上下文可以按预算生成较小的请求视图，完整会话历史仍由原 owner 保存。重建索引要核对来源完整性和实际重建方法；删除源码、卸载能力、逻辑失效和物理删除数据，各有自己的授权与处理边界。

源码和文档使用 Git 作为恢复点，同时保留用户未提交内容。正式操作可能改写或减少无法可靠重建的数据时，备份受影响部分及恢复所需的关联状态。备份质量通过隔离恢复、重新加载和关键路径检查来验证。[状态与权限规则](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/projectneed.md)。

### 验证目标与受保护结果

验证要同时证明目标行为已经实现，以及相关受保护结果仍然成立。选择能够直接观察本次风险的最小入口：真实 CLI/API、浏览器操作、真实组件集成、E2E，或者隔离实例中的重放。

Akashic 当前明确禁止新增或改写单元测试，也禁止把函数级 mock 和断言换名放进 scenario 或 E2E。既有测试与 CI 保留，验收预期继续来自已确认的需求和决定。[验证政策](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/decisions/0083-short-workflow-preserves-design-intent.md)。

持久化改动从真实入口触发，检查既有记录的完整内容、身份和顺序，并按风险观察写入尝试、失败路径和重新加载。以窗口缩减为例，需要确认模型输入缩小后，原消息仍完整可读，后续追加仍沿用正确序号。合法新增记录存在时，不要求整个数据库文件字节不变。[上下文保留合同](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/decisions/0002-context-reduction-is-a-nondestructive-projection.md)。

验证使用一次性 workspace、plugin home、config 和 HOME，并检查实际连接目标，避免验证进入正式运行状态。高风险行为保持重构，在基线和候选隔离实例中运行同组脱敏输入，核对持久写入、事件、外部调用、错误分类和用户可见结果。

### 执行相关静态检查

本地按改动选择相关静态检查。常用入口如下，`<base>` 使用本次记录的目标基线：

| 范围 | 检查入口 |
| --- | --- |
| Python 类型 | `.venv/bin/pyright --level error` |
| Python SDK | 在 SDK 项目范围执行 Pyright |
| 插件依赖边界 | `python scripts/plugin_boundary.py check --base <base>` |
| Yoyo 迁移 | `python scripts/check_yoyo_migrations.py --base <base>` |
| Control 协议 | `python scripts/generate_control_schema.py --check` |
| Host Bridge 协议 | `python scripts/generate_host_bridge_protocol.py --check` |
| 前端类型 | `npm run typecheck` |
| 文档 | 相对链接、规则一致性、代表任务阅读路径、`git diff --check` |

完整既有检查由 CI 执行，包括类型、生成协议、迁移、SDK、既有 Python 回归和前端类型检查。[当前 CI](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/.github/workflows/ci.yml)。

前端任务先读 `frontend/design.md`，再修改源码。交付前按范围运行类型检查和构建：

```bash
npm run typecheck
npm run build:chat
npm run build:web-plugins
```

后两条分别用于 Chat 和插件 UI 改动。布局、断点或 token 变化，需要固定 Chromium 截图验证；窄屏行为变化运行对应窄屏检查。截图保留在本地私有目录。[前端验收规则](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/frontend/design.md)。

### 保存验证与交接材料

验证记录应写清实际源码版本、命令、环境、观察结果与覆盖范围。静态检查、既有回归、功能、真实设备和部署证据分别报告。跨仓库组合固定参与方的源码、协议、制品和场景身份；任何输入变化，都要重新判断证据是否适用。未运行的层次明确标为未验证。

长任务在主要里程碑保存上下文交接材料，保留目标、完成标准、受保护状态、已确认事实、关键假设、决定、代码引用、验证状态、剩余工作和下一步。接手者沿引用恢复细节。[上下文交接模板](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/templates/context-handoff.yaml)。

## 第三步 提交清楚的改动并交由维护者评审

### 检查实际交付范围

交付前检查实际完整 diff，并获取目标分支最新状态，确认新的代码和工作手册变化没有使本次边界失效。重点关注新增权限、状态写入、删除路径、错误含义和外部效果。

每张 PR 默认新增约 **500 行以内**，删除量不限。新增量按实际 PR 的 `base...head` 计算。大任务优先拆成 stacked PR：每层包含一个完整、可以独立理解和验证的变化，base 指向前一层，保留依赖顺序与栈顶累计验证。无法合理拆分时，说明原因交由维护者决定。

架构与概念由维护者亲自评审。执行者负责符合已确认设计、报告具体问题并提供实际行为证据。[交付规则](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/docs/WORKFLOW.md)。

### 写清问题结果方案与验证

PR 正文面向没有读过会话的评审者，说明问题与结果、方案与理由、验证。Bug 写清触发条件和影响；方案解释关键取舍；验证记录实际操作与观察。存在数据、兼容性、外部效果或部署风险时，再补相关合同与恢复方式。[PR 模板](https://github.com/kachofugetsu09/akashic-agent/blob/50b93c7e778cfc301fbd75f9df9ec1a3041447ed/.github/pull_request_template.md)。

### 维护项目事实并完成交接

交付时同步维护被改变的项目事实：长期规则进入 `projectneed.md`，可复用设计选择进入 decisions，具体架构和迁移进入 design，完成事项从 NOW 删除，入口变化更新 INDEX。普通修复理由留在 PR。决策记录说明适用条件、选择理由、重要替代方案、代价和重议条件。

Writer 交接前结束写入，将允许范围内的修改提交成可引用 commit，或交接已经核对的 clean HEAD。交接记录仓库、worktree、分支、基线、HEAD、dirty state、下一位 owner 和允许路径。

提交、推送、修改 GitHub、合并和部署按已有授权分别执行。只读评审任务保持只读；建立 PR 本身不授予合并或部署权限。

## 可复制的任务启动说明

下面这段说明按当前规则整理，适合直接交给执行 Agent：

```text
完成任务：<具体目标>。

成功标准：
<用户可以观察或检查的结果>。

先按 docs/INDEX.md 阅读设计原则、相关需求、有效决策和真实实现。
说明本次改变什么、保持什么，以及准备如何验证。

在现有授权范围内自主完成实现和必要验证。
涉及持久状态时，核对实际增、改、减、负责模块和恢复方式。
需要改变现行语义或扩大授权范围时，说明理由、影响和选择。

按 docs/WORKFLOW.md 与相关领域规则执行。
验证应经过能够观察本次行为的实际入口，并检查相关受保护结果。

交付时说明问题与结果、关键方案理由、实际验证和未验证范围。
同步更新被改变的文档事实，并留下可以继续接手的代码与证据。
```
