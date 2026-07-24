# 0002 consolidation 暂由 MemoryRuntime 驱动,不交给 MarkdownMemoryMaintenance

- 状态:accepted(待 NOW.md 第 1 项完成后由新记录 supersede)
- 关联:`kirakira_agent/memory.py`、`kirakira_agent/coremem/markdown.py`、
  `kirakira_agent/coremem/services.py`

## 背景

从 Reference 移植的 `DefaultMemoryEngine._on_consolidation_committed` 会从 consolidation
窗口提取长期 profile/preference/procedure。但 `ConsolidationCommitted` 的唯一发射者是
`MarkdownMemoryMaintenance`,而它没有被接线,因此该 handler 从未触发,属于死代码。

目标是让这条能力通电。可选路径有两条:把 consolidation 驱动权交给 maintenance
(最贴 Reference),或让现有驱动方发出该事件。

## 决定

保持 `MemoryRuntime` 为 consolidation 的唯一驱动,由它在归档提交后发出
`ConsolidationCommitted`。`MarkdownMemoryMaintenance` 以 `event_bus=None` 构建,
不订阅 `TurnCommitted`,只提供四文件读取。

广播失败只记日志:归档已经写盘,不因下游提取失败而回滚。

## 理由

两边都订阅 `TurnCommitted` 会重复归档,并同时推进 `last_consolidated` 游标。

更关键的是旧路径承载一条安全行为:待归档消息超过阈值且 consolidation 无法推进时,
`PassiveTurnPipeline._guard_memory_context` 会拒绝本轮,避免静默丢历史。它依赖同步
schedule + wait 的语义,而 maintenance 是异步队列,直接切换会让这个保护失效。

## 替代方案

- 直接切到 maintenance 驱动:被否决,会同时引入重复归档与保护失效两个回归。
- 继续让 handler 保持死代码:被否决,移植过来的能力不通电等于没有这个能力。

## 影响

引擎的长期事实提取现在会真实触发。与 Reference 的结构差异集中在"谁驱动 consolidation"
这一点,已记入 [NOW.md](../NOW.md) 第 1 项,连同移交所需的前置条件与验收。

## 验收

- consolidation 提交后引擎 handler 收到事件,载荷含 history 条目、source_ref 与会话作用域;
- 无 event_bus 或无 history 条目时不发事件;
- 广播失败不把异常抛回 consolidation。
