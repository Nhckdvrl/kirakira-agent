# 0003 consolidation 驱动权移交给 MarkdownMemoryMaintenance

- 状态:accepted
- supersedes:[0002](./0002-consolidation-driver.md)
- 关联:`kirakira_agent/coremem/markdown.py`、`PassiveTurnPipeline._guard_memory_context`、
  `kirakira_agent/coremem/services.py`

## 背景

[0002](./0002-consolidation-driver.md) 记录了暂不移交的两个理由:重复归档,以及旧路径
承载的 context guard 依赖同步 schedule + wait 语义。现在两个理由都可以消除。

## 决定

`MarkdownMemoryMaintenance` 重新订阅 `TurnCommitted`,成为归档与近期上下文刷新的驱动方。
runtime 侧的 `consolidate_turn` 与 `schedule_consolidation` 调用改为**只在没有承重维护器时**
执行,因此两条路径不会同时归档。

context guard 改用 `maintenance.consolidate(ConsolidateRequest(force=True))`:它是可等待的,
并且取与队列维护同一把 session 锁。

## 理由

重复归档的顾虑通过"有维护器就不走旧路径"消除,判定收敛在 `_markdown_maintenance()` 一处。

guard 的同步性顾虑不成立:`consolidate()` 本身就是可等待的直接调用,不经过队列;它取
session 锁,因此与队列里的维护任务串行,不会互相插队——这比旧路径的 `schedule + wait`
更强,旧路径两个调用之间没有互斥保证。

维护器未绑定 session 生命周期(`_get_session is None`)时不算承重,回退旧路径。这样
未配 embedding 或未注入 session_manager 的部署不受影响。

## 影响

`MemoryRuntime` 的 `consolidate_turn` / `schedule_consolidation` / `_consolidate_session`
在正式部署路径上不再被调用,但暂未删除:它们仍是无维护器时的回退。彻底删除需要先确认
没有部署依赖回退路径,列在 [NOW.md](../NOW.md)。

## 勘误(移交当次遗漏,已修复)

移交时只改了归档的触发方,漏了 turn 开始前那句无条件的 `memory.wait_for_session`——
它仍在等旧 runtime 的任务集,而那里已经不再有任务,等于"下一轮等上一轮归档收口"
这条保证失效。归档会推进 `last_consolidated`,不等就读会拿到错位的历史窗口。

修法:给 `MarkdownMemoryMaintenance` 增加 `wait_for_session()`,runtime 在有承重维护器时
等它。超时只放弃等待、不取消任务——取消会让归档停在半途,比等不到更糟;上一轮归档
失败也不把异常抛进本轮 turn。

## 验收

- 有承重维护器时 runtime 不调用旧归档路径,同一 session 不被归档两次;
- 待归档超阈值且归档未推进时,turn 仍被拒绝并给出提示,不静默丢历史;
- 归档推进后保存 session,游标不回退;
- `consolidate()` 抛异常时不外抛,按"未能推进"处理并拒绝本轮;
- 无维护器时行为与移交前一致;
- turn 开始前等待的是**正在驱动归档的那一方**,等待超时不取消归档任务,
  上一轮归档失败不影响本轮 turn。
