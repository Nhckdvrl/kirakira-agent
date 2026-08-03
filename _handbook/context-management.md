# 上下文治理合同

## 真相与投影分离

`sessions.db/messages` 是完整历史的唯一权威。正常 `SessionManager.save()` 只能追加；删除、覆盖或
缩短历史会直接失败，数据库保持原样。JSON 文件只可能作为旧数据导入源或可读镜像，不参与恢复
当前运行态。

模型看到的是**当前请求投影**，不是数据库本体：

```text
持久 Session（完整、append-only）
  → 读取当前未归档历史
  → render 具名 PromptBlock
  → 叠加 Context Frame、当前消息、工具 schema、图片预算
  → 本次模型请求
```

因此“为了过 context limit 缩小历史”和“删除旧会话”是两件完全不同的事。前者可重算、只活在
当前 attempt；后者只有显式 destructive 管理操作才允许。

## Prompt 结构

稳定 section 包括 identity、behavior rules、skills catalog、self model、长期记忆与 session context；
动态 Context Frame 包括 recent context、active skills、retrieved memory、turn injection 与 plugin
hints。动态块带系统标记并位于历史之后、当前用户原文之前，明确声明它不是用户陈述。

稳定 section 按 workspace 与内容签名缓存。Skill 默认只暴露目录；用户点名 `$skill-name` 或声明
`always: true` 时才加载正文。

## 预算与降级

```text
input_budget = floor(context_window × effective_context_percent) - max_tokens
```

估算覆盖 system、所有消息字段、工具 schema、图片和输出预留。估算器是保守近似值，trace 标记
`estimate_quality=approximate`；真实计费以 provider usage 为准。

外层 attempt 每次都重新经过 prompt hooks：

1. full；
2. 去 skills catalog；
3. 去 recent context；
4. 去 long-term memory；
5. 去 retrieved memory；
6. 历史投影缩到 50%；
7. 历史投影缩到 0。

切片回退到 user 边界，不从半组 tool call 开始。2026-08-04 的 DeepSeek 在线验证实际在 60 条
持久历史上降到 0 条投影后完成请求，同时 60 条旧消息 ID 全部保持不变。

## 同一 ReAct 内的工具批次压缩

长任务在当前请求内累积多个完整工具批次时，`QueryCompactor` 可以让模型总结已闭合的旧前缀，
保留至少最新批次，再用 `context_compact` 协议消息继续。压缩摘要本身也计入模型请求与 usage。

只压缩完整 assistant tool-call + tool-result 批次；仍在运行的 Shell 起始批次会被 pin，直到
`write_stdin/task_output/task_stop` 表明执行已结束。提交时只把压缩元数据随新的 assistant 消息
追加；不会回写旧消息。下轮 replay 根据 `react_compaction` 重建模型历史。

## 用量与可观测性

每个模型请求都计数，包括 provider 没返回 usage 的请求。聚合字段：

- input/output tokens；
- cached input tokens（含 DeepSeek prompt cache hit）；
- reasoning output tokens（provider 提供时）；
- request count、covered request count；
- coverage：`exact` / `partial` / `unavailable`。

缺少遥测是 `unavailable`，不是 0。每条 assistant 消息保存 `context_trace`：所有 attempt、section
大小、预算、所选计划、ReAct 估算、模型 usage 与请求数。控制面事件同时发布准备和预算信息。

## Consolidation 边界

Markdown maintenance 使用 `last_consolidated` 推进归档游标。下一轮读取前会等待同 session 上一次
归档收口；无法推进时明确失败，不能通过删历史掩盖。结构化长期记忆与 Session 历史是不同 owner。

## 不变量

- Reference checkout 不参与上下文生成。
- 持久历史不可因 prompt 压力被删除。
- 检索记忆不可伪装成用户原文。
- 重试必须重新渲染，不复用旧计划的 prompt hooks 产物。
- 工具 schema、图片、摘要请求与输出预留都必须计入预算/请求统计。
- provider 已产生可见 streaming delta 后不得切换备用模型。
