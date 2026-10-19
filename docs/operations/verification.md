# 验证记录

本文只记录已经实际执行过的验证。功能现状见[当前状态](../status/current.md)，不要从测试数量反推能力范围。

## 2026-10-19：原文证据与独立恢复（最新）

同一 `.venv` / Python 3.14.6 全量 **702 passed、4 subtests**（51.78 秒），22 条既有 Telegram 警告。
新缓存按原文、内容摘要和完整模型空间固定在线向量；真实 BGE 的 24 份历史仅从 Session 源库
重建，全部学习表与完整消费契约相等，网络连接尝试为 0；234 条召回记录均可完整回源。

三张冻结公开历史图上的证据、正文与顺序不变，恢复源库连接由 649–904 次降为 2 次，query
由 18–20 次降为 1 次。旧/新分数最大差 `1.11e-16`，来自修复恢复时重复归一化；新实现自身
重启前后记录严格相等。未修改固定算法文件或既有迁移。

24 道合成合同题、七组共 168 次 reader：双通道/Dense/BM25/RRF 各 24/24，Mem0 23/24，
其唯一失败为正确值返回成字符串而非数组；不当作记忆内容失败。三轮真实 Agent 严格断言
5/6、5/6、6/6，完整保留；第二轮失败的最终值与原文回源均正确，但输出额外解释了废弃码。

构建、CLI、算法 pin、迁移不可变、源码与结果摘要、文档链接和提交密钥检查通过。
详见[专项报告](./evaluations/2026-10-19-episodic.md)与[项目表达](../engineering/memory-design-and-evidence.md)。

## 2026-10-19：来源会话 Scheduler 与记忆复测

同一项目 `.venv` / Python 3.14.6 最终全量 **694 passed、4 subtests**（51.06 秒），22 条既有 Telegram 警告。
新 Runtime 场景首轮 48/48、统一 admission 后 52/52；Core/Wake/Drift/记忆工具另有 23/23。
全部真实生成使用 Step5 Token Plan。最终 Scheduler 批次观察到 238 次尝试、144 次成功响应；
失败尝试经有界重试恢复，未删除失败记录。

公开 272-turn 状态轨迹与上游完整学习状态及九个查询逐项一致，1,024-turn 成本实验、三处发布故障、
两种哈希种子、21 项数学性质及 672 个矩阵形状通过。原 adapter 遗漏宿主精确 dense 通道，已补齐并
保留总记录预算；原图 24 题诊断为 18/24，另 12 道新问题为 11/12，BM25 同为 11/12。
新读出真实记忆工具链另测 6/6。旧固定 36 题、24 题保留集、12 题读出验证及真实 Mem0 OSS
同题子集独立记录，共 72 个不同 question ID，不与重复次数混算。
Mem0 空库写入 12/12、74 次模型响应成功；同 12 题 Mem0、双通道与 BM25 均为 9/12。
本批质量实验共 612 组回答/判分，零运行错误；含 Mem0 写入的可见成功响应为 1,298 个、返回
usage 合计 5,114,677 tokens，全部 Token Plan，不包括 Runtime 与中止预检。
构建、CLI、迁移不可变、算法 pin、文档本地链接与密钥扫描通过。

详见 [Runtime 报告](./evaluations/2026-10-19-scheduler.md)、[记忆报告](./evaluations/2026-10-19-memory-followup.md)。

## 2026-10-19：统一 Python 环境（历史）

项目固定 Python 3.14.6，使用根目录 `.venv`。`.python-version` 与 CI 的
`python-version-file` 共用同一版本来源，删除 3.11/3.14 矩阵；支持范围收敛为 3.14 系列。
`uv.lock` 删除旧 Python 的解析分支和重复 numpy 版本，当前 3.14 环境的依赖版本保持。

实际在原项目 `.venv` 执行 `uv sync --frozen --group dev`、`uv lock --check`、
`uv run --frozen pytest -q`、构建与 CLI 帮助检查。全量结果：`662 passed, 22 warnings, 4 subtests passed in 50.32s`。
首次原目录回归的两项失败来自旧包残留字节码目录；清理 147 个缓存文件后复验，未放宽目录约束。
下方双版本记录为历史验证，不再作为当前环境要求。

## 2026-10-19：提供者归属与 Runtime 回收（历史）

Python 3.14.6 全量 **662 passed / 4 subtests**（49.39 秒），Python 3.11.15 全量同样通过（48.65 秒）。
两版均有 22 条既有 Telegram 弃用警告。删除 10 个纯容器测试，新增 11 个真实组合与资源/工具边界测试；
最后插件 API 与影响选择器增量 17 项通过。

真实 Step5 Core/Wake 决策 12/12、Drift 4/4、Wake 整链 1/1、记忆工具 6/6，共 **23 个场景全部通过**。
全部使用 Token Plan 和原生 reasoning_effort=medium。新增记忆工具场景使用本地固定向量，验证显式工具、
原文解析与重启链路，不作为质量评测。构建、CLI、主要修改模块 Ruff F/I 和 diff 检查通过。
装配/启动失败回收、取消物理排空及错误后继续关闭均由实际组合入口回归覆盖。

详见[本轮报告](./evaluations/2026-10-19-ownership.md)与其中的原始结果。

## 2026-10-19：组件、恢复与真实记忆实验（上一轮）

Python 3.14.6/Linux；上游 pin 为 `d85b00c233686b68b082052b1b20123905aeab13`。

| 验证 | 结果 |
| --- | --- |
| 最终全量回归 | **661 passed，4 subtests passed**，49.82 秒 |
| Python 3.11.15 复验 | **661 passed，4 subtests passed**，49.59 秒；25 条既有依赖警告 |
| 警告 | 3.14 为 22 条既有 Telegram 弃用警告 |
| 原生 Step5 功能 | Core 12/12、Drift 4/4、Wake 整链 1/1；重复 tick/重启和本地回执均验证 |
| 模型路由 | 只使用 Token Plan `/step_plan/v1` 与 `step-5-preview`，原生 reasoning_effort；未用现金接口 |
| 记忆质量 | 两轮固定 36 题、四组及 oracle，共 360 组回答/判分；初始失败完整保留 |
| HTTP/SSE 与恢复 | 真实 loopback 503、断流、截断、限次、取消；完成工具不重放 |
| 记忆状态 | 原始 SQLite 消费、同时间戳顺序、checkpoint 尾部重放、原文损坏拒绝、独占 writer、离线重建 |
| 组件生命周期 | 显式依赖、代际借用、缺失依赖撤销、任务 finally 与取消 closer 的继续清理 |
| 制品与独立性 | sdist/wheel、独立安装环境导入与 CLI、Akasha2 许可证/来源、无 Reference 依赖 |
| 静态与文档 | 本轮主要新模块 Ruff 检查、diff、文档本地链接、来源 SHA 与密钥扫描通过；不声称全项目类型检查 |
| 真实用户状态 | 未迁移历史库或修改私人配置；在线投递只使用测试本地回执 |

首次新增远端矩阵捕获了 Python 3.11 导入错误：provider 工厂替换后，主动构造函数仍注解为未导入的旧
client 类型；Python 3.14 的延迟注解求值没有触发该错误。已改为 `ModelClient` 协议，并完成上述 3.11 全量复验。
CI 的 Node 24 Actions 已按官方发布固定 SHA；矩阵保留两种 Python 的独立结果，不再因另一项失败提前取消。

结果、负面结论、协议修正与逐题文件见[实验报告](./evaluations/2026-10-19-memory.md)。
原生配置 Dense 为 28/36，v1/Akasha2 均为 27/36，不把接入新算法等同于质量提升。
所有收费模型验证使用用户授权的 Token Plan；免费 CPU embedding 单独缓存。

## 第三轮 core 与工作流修订（历史）

2026-10-03，Python 3.14.6/Linux；上游固定点仍为 `7cf10136932d5fe0205727c803c00861272db0b8`。
从当前源码复制到不含真实 config、workspace、Reference 的临时目录执行全量回归。

| 验证 | 结果 |
| --- | --- |
| 最终全量回归 | **637 passed，4 subtests passed**；76.12 秒 |
| 警告 | 22 条既有 Telegram 弃用警告 |
| 新增执行合同与服务注入回归 | 22 passed；模型为确定性模拟，本地 Session/Execution SQLite 为真实组件 |
| 核心静态检查 | Ruff I/F 检查通过；不声称全项目类型检查通过 |
| 迁移不可变检查 | 对 origin/main 通过；本轮未修改既有迁移 |
| 构建 | sdist、wheel 成功；核对最终制品中的共享执行器、日志、兼容入口和 inspector 与当前源码相同；不含真实 workspace |
| 实际 CLI | 主入口与 inspector 帮助通过；同步 Agent 位置参数 → 共享执行器 → SQLite → 独立 inspector CLI 闭环通过 |
| 文档 | 仓库相对链接检查通过；修订稿 15 个固定提交上游链接均对应实际文件 |
| diff | `git diff --check` 通过 |
| 真实 provider / 外部渠道 / 正式部署 | 未运行 |

首次完整回归为 631 passed / 3 failed，原因是服务注入测试把 event bus 设为空对象，无法订阅
下沉到 pipeline 的中断事件。改为实际 EventBus，保留原服务注入断言；最终完整结果如上。
完整回归之后保留了同步 Agent 原有位置参数调用方式，另以实际执行与 inspector CLI 闭环验证。

新增场景覆盖 intent 先落库再执行工具、日志写入失败阻止调用、工具中断的未知结果、启动恢复不重放、
模型拒绝上下文后不重跑副作用、stream worker 取消收束、直接入口排队与打断、等待者取消不释放当前
owner、钩子之后重新检查 grant、scoped 日志、发送前必要写入失败不投递。

这些证据不等同于 Akashic 完整持久程序续跑、真实模型质量、外部渠道 exactly-once 或部署验收。
工作流文档为源码阅读与文档核对结果，没有运行上游全量检查或私有组合。

## 第二轮完整结果

2026-10-02，Python 3.14.6/Linux；上游对照点 `7cf10136932d5fe0205727c803c00861272db0b8`。
仍在不含真实 config、workspace、Reference 的隔离源码副本执行。

| 验证 | 结果 |
| --- | --- |
| 全量测试 | **625 passed，4 subtests passed**；46.63 秒 |
| 警告 | 22 条既有 Telegram 弃用警告 |
| 变更影响选择 | 17 个测试文件，缺失为 0；包含在全量回归中 |
| 迁移不可变检查 | 对 origin/main 通过；仅新增 publication 迁移 |
| 构建与 CLI | sdist、wheel、主 CLI 与 replay CLI 检查通过 |
| 文档与 diff | 相对链接、`git diff --check` 通过 |
| 真实 provider / 外部渠道 | 未执行；测试使用隔离数据库和模拟模型 |

新增验证覆盖 Session 与 publication 同事务回滚、漏通知后重启追赶、失败前驱阻塞后继、首次 cutover、
取消与固定向量重试、维护任务取消、源库身份变化、重建后新消息续接；以及会话/忙闲/规则/候选四类
模型执行期间变化、渠道执行期间候选更新、旧版本不能消费/检查/反馈新版本、Drift 草稿过期。

## 第一轮完整结果

日期：2026-10-02；Python 3.14.6，Linux。对照本地 `b6c4143` 基线，测试在只复制版本控制源码与本轮新增代码的
隔离目录执行，未携带真实 config、workspace、Reference 或原仓库旧命名空间的 pycache。

| 验证 | 结果 |
| --- | --- |
| 修改前隔离基线 | 586 passed / 2 failed；失败为 CLI 验参顺序与测试 workspace 污染 |
| 修改后全量测试 | **609 passed，4 subtests passed**；44.18 秒 |
| 警告 | 22 条既有 Telegram `retry_after` 弃用警告 |
| 构建 | sdist 与 wheel 成功；新增 scoped/policy/cycle/replay 模块包含在包中 |
| CLI smoke | `python -m kirakira_agent --help`、`python -m scripts.replay_akasha --help` 成功 |
| 变更影响选择 | 选出 14 个合同测试文件，缺失文件为 0；已包含在全量测试中 |
| 文档与 diff | 仓库文档相对链接有效；`git diff --check` 通过 |
| 真实 provider / 外部渠道 | 本轮未执行，没有把 mock 当成在线结果 |

新增重点证据：

- 明文 JSON 不能绕过决策工具；未知/重复候选、冲突终结批次不能授权投递。
- 执行器会阻止已注册但未授权的工具，也检查同一批次内的动态授权收窄。
- 冷却期间事件在重启后仍可判断；有效 skip 只检查一次，错误按持久时间延后重试。
- Drift 没有 finish 时不提交草稿。
- 所有记忆查询不修改学习图；重复提交不强化，稳定消息 ID 不漂移到重复文本的新消息。
- 模拟写入中途磁盘错误：图与收据一起回滚；重试复用固定向量。
- 跨 session 和中途重启后的线上状态与同 cycle 重放一致。
- FTS 不能看到未来消息；配置变化、非法向量、缺失重放向量明确失败；新库不覆盖已有文件。

这证明已覆盖的运行合同，不证明 Akasha2 parity、长期质量提升、真实主动体验或所有故障自动恢复。
尚未覆盖项以[当前状态](../status/current.md)为准。

## 历史记录：2026-08-15

以下为重构前仓库已有记录，本轮没有重新执行其中的在线验证。

环境：2026-08-15，当时仓库的 Python/uv 环境。

| 验证 | 结果 |
| --- | --- |
| 全量测试 | 588 passed，另有 4 个 subtests 通过 |
| 构建 | 成功 |
| CLI smoke | 成功 |
| 删除/移走 Reference 后测试、构建、CLI | 成功 |
| DeepSeek 在线验证器 | 成功 |

Reference 独立性验证证明外层 Kirakira 不 import、不读取、也不要求本地存在 `Reference/`。Reference 仅在
开发期用于人工对照；删除它后项目行为不应改变。

## 历史 DeepSeek 在线验证覆盖（2026-08-15）

通过已配置的 DeepSeek API 实际执行 `kirakira-verify-online`，覆盖：

- 普通模型响应与 provider usage；
- forced tool call 协议，参数为 `probe-ok`；
- 默认工具 `read_file` 的真实 AgentLoop 调用；
- 1024 维 embedding 写入与查询；
- 长 session 真实触发 context compaction，生成 generation 1 ledger；
- 摘要覆盖 52 条 source messages，保留 246 条 provider 投影消息，原始 message id 和顺序不变；
- 摘要的 Goal / Progress / Decisions / Next Steps / Critical Context 标题合同通过；
- context trace 中模型请求 usage coverage 为 `exact`；
- Akasha v1 摄入、检索、回源，并由模型在最终回答中消费证据。

该次 provider probe 返回 input 17、output 7 tokens。这个数字只证明遥测字段和归一化链路可用，不作为
性能基准。

## 常用命令

```bash
uv run pytest -q
uv build
uv run python main.py --help
uv run kirakira-verify-online
```

在线验证会读取本地 `config.toml` 中的 provider 和 embedding 配置，但不会打印 API key。验证器使用临时
workspace，不污染真实 Session、Memory 或 Proactive 数据。

## 结果口径

- **已测试**：合同、边界和回归由自动化测试覆盖；
- **已在线验证**：真实 provider 和真实 AgentLoop 已执行；
- **未验证**：没有把 mock 或静态检查包装成实弹结果。

## 尚未覆盖的实弹范围

- Telegram、QQ、QQBot 等外部 Channel 的真实账号端到端投递；
- 完整 LongMemEval 的模型 Judge/F1/EM 质量评测；
- Akasha 新版本的全量图行为 parity；
- 前端、移动端和轨迹可视化页面。

这些边界不影响当前后端架构完整性，但不能宣称已经做过在线端到端验收。
