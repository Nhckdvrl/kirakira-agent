# 0010：工具提供者归属与 Runtime 回收

日期：2026-10-19。状态：已实现。

## 决定

组合根负责选择与连接提供者，公共插件 API 与加载/更新实现分离。工具处理器只持有自己使用的依赖。
这延续 Akashic ADR0102 的职责方向，不表示移植了其全部组件系统。

- `bootstrap/runtime.py` 是异步 Runtime 组合入口；`bootstrap/app.py` 处理 CLI、UI 选择与 serve。
- 渠道、Wake/Drift 和重启工具分别在 `bootstrap/channels.py`、`proactive.py`、`restart.py` 装配。
- `agent/tools/builtins.py` 提供工作区工具；网页读取与搜索由 `agent/tools/web.py` 实现。
- `plugins/memory_tools/tools.py` 拥有记忆 schema、证据格式、归档与原始消息工具。
  `bootstrap/tools.py` 显式组合工作区与记忆提供者；Core 和工作区工具不反向导入此组合根。
- `agent/plugins/api.py` 定义稳定的 Plugin API 3 类型。`manager.py` 使用这些类型，不再拥有其定义，
  也不再接收未使用的 session/memory 参数。公共 `agent.plugins` 导入路径保持。
- 删除只用于转发或测试的 `agent/looping/ports.py` 及占位字段。Pipeline 直接接收 SessionManager；
  有实际行为的 MemoryServices、版本能力与代际租约继续保留。
- 删除未被 CLI 使用的同步 `build_agent/repl` 分支；公开 Agent 执行器仍由现有测试与共享 reasoner 承载。

## 记忆边界

工具集合来自选中引擎的 `tool_profile()`；Akasha 没声明的 memorize/forget 不得补回。
记忆写入、遗忘、归档和自定义工具的 risk/search_hint 随工具注册，不能统一标成 read-only。

Drift 的档案读取对象与记忆引擎分开注入。后台只开放 recall、原文搜索和原文读取，
不给予显式记忆写入、强化、遗忘或归档工具。缺少承重引擎时，不把 Markdown 档案伪装成词法 MemoryRuntime。
这是记忆工具授权范围；Drift 的工作区文件与 Shell 工具仍按其执行策略运行。

## 回收边界

组合入口使用 AsyncExitStack，在构造成功后立即登记资源。全部装配成功才把所有权交给 CoreRuntime；
中途失败则按相反顺序关闭已取得的资源。它不替代各构造器对自身半成品的清理责任。

Runtime 保存自己启动的 task，不依赖调用方重新提供完整列表。启动失败走同一关闭路径。
关闭先阻止新任务并物理排空生产者，再排空投递、关闭工具、插件、记忆和存储。
每个关闭动作都尝试执行；错误在最后聚合上抛。并发/重复关闭共享 ResourceScope，取消等待者仍等待物理关闭。
ToolRegistry 使用同一资源 scope 合同，单个 callback 失败不能跳过其他 callback。
手动单次主动执行即使未启用主动链路或渠道启动失败，也关闭 Runtime。

## 验证与限制

真实组合入口覆盖装配失败、渠道启动失败、并发关闭、取消关闭、失败后继续关闭、生产者先退出、
空 task 列表以及单次命令退出。记忆提供者覆盖引擎工具面、risk、后台拒绝写入与原文回源。
真实 Step5 的功能结果与固定向量限制见[本轮记录](../operations/evaluations/2026-10-19-ownership.md)。

本次不更换记忆算法，不迁移私人数据，不把文件拆分等同于完全中立的 Core。
Scheduler 仍是原有 instant/soft 合同；来源会话、任务来源历史、持久触发结算与插话续跑尚未整体迁移。
