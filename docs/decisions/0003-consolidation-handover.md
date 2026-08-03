# 0003 consolidation 驱动权移交给 MarkdownMemoryMaintenance

- 状态:accepted
- supersedes:[0002](./0002-consolidation-driver.md)
- 关联:`core/memory/markdown.py`、`PassiveTurnPipeline._guard_memory_context`、
  `core/memory/services.py`

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

旧 consolidation 路径已删除(`memory.py` 1039 → 823 行):`consolidate_turn`、
`wait_for_session`、`schedule_consolidation`、`_consolidate_session` 及其专用辅助
(`_known_memory_digest`、`_parse_consolidation_json`、`_extract_explicit_memory`、
`_latest_user_source_ref`、`_last_assistant_used_memorize`)。

没有记忆服务包的最小构造(测试等)不再有归档能力:此时 guard 直接放行而不是拒绝每一轮——
既无法推进也无从等待,拒绝只会让最小构造不可用。生产始终有维护器,保护完整生效。

## 勘误(移交当次遗漏,已修复)

移交时只改了归档的触发方,漏了 turn 开始前那句无条件的 `memory.wait_for_session`——
它仍在等旧 runtime 的任务集,而那里已经不再有任务,等于"下一轮等上一轮归档收口"
这条保证失效。归档会推进 `last_consolidated`,不等就读会拿到错位的历史窗口。

修法:给 `MarkdownMemoryMaintenance` 增加 `wait_for_session()`,runtime 在有承重维护器时
等它。超时只放弃等待、不取消任务——取消会让归档停在半途,比等不到更糟;上一轮归档
失败也不把异常抛进本轮 turn。

## 勘误二:移交静默停掉了两个行为(核对后确认)

逐处核对回退路径时确认:`build_memory_services` **无条件**构建 markdown 并绑定 session,
因此 `_markdown_maintenance()` 在配了 embedding、没配 embedding、memory 关闭三种配置下
都返回承重维护器。旧 `consolidate_turn` / `schedule_consolidation` 的回退分支从移交那一刻
起就不可达,连带停掉两个原本由 `consolidate_turn` 承担的行为:

1. **`HISTORY.md` 不再追加**。核对结果:该文件只被写、没有任何读取方(`memory_admin`
   只在初始化时创建它),属于审计留痕,停写不影响运行语义。

2. **`"请记住:X"` 的正则自动抽取不再执行**。配了 embedding 时由
   `PostResponseMemoryWorker` 的 explicit_memories 覆盖,且是 LLM 判断,比正则更准;
   没配 embedding 时无人接管——此时结构化记忆本就按门控关闭,模型仍可显式调用
   `memorize` 工具写入,只是少了这条自动捕获的兜底。

两项都记在这里而不是悄悄留着:移交时只检查了"谁触发归档",没检查"归档顺带做了什么"。

## 验收

- 有承重维护器时 runtime 不调用旧归档路径,同一 session 不被归档两次;
- 待归档超阈值且归档未推进时,turn 仍被拒绝并给出提示,不静默丢历史;
- 归档推进后保存 session,游标不回退;
- `consolidate()` 抛异常时不外抛,按"未能推进"处理并拒绝本轮;
- 无维护器时行为与移交前一致;
- turn 开始前等待的是**正在驱动归档的那一方**,等待超时不取消归档任务,
  上一轮归档失败不影响本轮 turn。
