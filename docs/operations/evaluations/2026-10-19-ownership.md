# 2026-10-19：提供者归属、失败回收与记忆工具实测

本轮继续参考 Akashic `d85b00c233686b68b082052b1b20123905aeab13` 的提供者归属和资源生命周期思路。
本轮再次 fetch 后，上游 main 仍为此版本。实现合同见 [ADR0010](../../decisions/0010-provider-ownership-and-runtime-disposal.md)。

## 已完成

- CLI 与 Runtime 装配分离，渠道、主动链路、重启工具有独立装配入口。
- 记忆业务从工作区工具移出；网页工具独立；完整工具面由组合根显式装配。
- Plugin API 3 类型从管理器移出，保留公共导入与 context 接口；管理器删除未使用的 session/memory 参数。
- 删除 LLM/Session/Context 服务容器、占位字段和未使用的同步 CLI 分支，改为直接注入实际依赖。
- Drift 不再把 Markdown 档案当作词法 MemoryRuntime；只提供引擎检索和原文工具。
- 记忆写入、强化、遗忘与归档的风险元数据正确保留。
- 装配失败关闭前面取得的资源；渠道启动失败回收后台任务。
- Runtime 自己保存 task 所有权；并发/取消关闭等待同一物理回收。
- 停止生产者后再关队列、工具和数据库；单个关闭动作失败仍尝试其余动作，最终聚合报告。
- 单次主动命令未启用或渠道启动失败时仍关闭 Runtime；工具注册表使用统一资源 scope。

## 本地验证

| 检查 | 实际结果 |
| --- | --- |
| Python 3.14.6 全量 | 662 passed，4 subtests passed，49.39 秒 |
| Python 3.11.15 全量 | 662 passed，4 subtests passed，48.65 秒 |
| 警告 | 两版均有 22 条既有 Telegram 弃用警告 |
| 最后增量检查 | 17 passed；覆盖插件公共 API 与更新后的影响选择器 |
| 静态检查 | 修改的主要生产模块通过 Ruff F/I；不声称全项目类型检查 |
| 构建 | sdist / wheel、主入口和记忆验证器帮助检查 |

原先只检验服务容器字段的 10 个测试删除，新增 11 个真实组合/资源/工具边界测试。
数量变化不是功能提升的证据；新增测试实际覆盖装配失败、渠道失败、并发/取消关闭、callback 失败、
生产者排空、空 task 列表、单次退出、只读授权、工具 profile 与原文解析。
首次全量有 4 个失败，均是 schema 测试仍从已移走的旧模块导入；已更新到实际提供者并完成全量复验。

## 真实 Step5：23 个场景全部通过

全部聊天请求使用 `https://api.stepfun.com/step_plan/v1`、`step-5-preview`，原生 `reasoning_effort=medium`。
密钥从仓库外受限文件读取；不写入配置、prompt、结果或版本控制。未调用现金端点或收费 embedding。
投递使用隔离测试内的本地回执，未向私人聊天发送消息。

| 链路 | 通过 | 内容 |
| --- | --- | --- |
| Core / Wake 决策 | 12/12 | 六条实际工具链、其中三次工具完成后注入网络失败，四种 share/skip、兴趣估计、实际 Runtime 文件读写与重启历史 |
| Drift | 4/4 | 静默、草稿投递、用户新输入使草稿过期、暂停提案 |
| Wake 整链 | 1/1 | fetch、兴趣池、决策、投递、反馈，重复 tick 与重启去重 |
| 记忆工具 | 6/6 | 词法显式记住/召回/遗忘；Akasha 与 Akasha2 各自召回取原文、重启后再执行 |

记忆工具场景在三个隔离目录写入随机测试事实。最终 release code 仅存在于原始消息中，
模型须调用 `recall_memory` 和 `fetch_messages`，再回答修正后的随机值。
两代引擎关闭并重新构造后，再次走完整工具链。显式写入/遗忘只在支持这些能力的词法回退上验证，
不为 Akasha/Akasha2 伪造写工具。

这 6 个场景使用 `offline-hash-v1` 本地固定向量，只证明工具、原文、权限和恢复链路，**不是语义检索质量评测**。
本轮没有修改两代记忆算法，也没有重新跑公开数据集或改写此前结果。
[上一轮公开样本实验](2026-10-19-memory.md)的原生结果仍为 Dense 28/36、v1/v2 各 27/36；Akasha2 保持 opt-in。

## 重跑

`--key-file` 指向你自己的仓库外 Token Plan 密钥文件。每条命令使用新的工作目录，不能指向真实 workspace。

```bash
uv run python -m eval.scenarios.decisions \
  --key-file /absolute/path/step-plan.key \
  --workspace /tmp/kirakira-core-new --output /tmp/kirakira-core.json
uv run python -m eval.scenarios.drift \
  --key-file /absolute/path/step-plan.key \
  --workspace /tmp/kirakira-drift-new --output /tmp/kirakira-drift.json
uv run python -m eval.scenarios.wake \
  --key-file /absolute/path/step-plan.key \
  --workspace /tmp/kirakira-wake-new --output /tmp/kirakira-wake.json
uv run python -m eval.scenarios.memory \
  --key-file /absolute/path/step-plan.key \
  --workspace /tmp/kirakira-memory-new --output /tmp/kirakira-memory.json
```

逐场景结果、测试输出和来源摘要保存在[本轮制品](../../../eval/results/2026-10-19-ownership/README.md)。
这些场景没有覆盖真实 Telegram/QQ 账户投递、完整业务持续运行或所有 provider 热替换。
Scheduler 仍为旧 instant/soft 与隔离会话；完整迁移需把来源会话、历史投影、触发结算和插话恢复一起完成。
