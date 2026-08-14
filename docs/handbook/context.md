# 上下文治理

完整历史永远留在 `sessions.db/messages`；模型看到的只是本次请求的工作投影。上下文压缩不会更新或删除原始 message、tool result 和 tool trace。

## 请求门禁

每次调用主模型前都经过 `ContextCompactor.prepare()`。估算范围包括 system prompt、记忆与检索、session 历史、当前请求、tool result、工具 schema 和协议开销。

```text
soft_limit = floor(context_window × 0.74)
hard_limit = context_window - max_output_tokens
```

达到任一边界就尝试压缩，不等到窗口完全用尽。Provider 返回精确 input usage 后，同一请求的后续估算会优先使用“精确值 + 新增消息估算”。

## 完整历史单元

Session 历史按 `CommittedContextUnit` 组织，不再按消息条数切 `history[-N:]`。一个单元包含完整的 user 到 assistant 交互，assistant 内的 tool call 和全部 tool result 一起闭合。正在运行的 shell execution 会锁定切点，不会从执行中间切断。

压缩时从最新单元向前累积，至少保留 `20_000` tokens 原文。这是目标下限；因为不拆单元，实际保留量可以更大。

## 滚动结构化摘要

较旧的已闭合单元由模型生成 task-state 摘要，严格使用以下标题：

```text
## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Next Steps
## Critical Context
```

摘要保留文件路径、symbol、命令、错误、数值、工具结果、外部副作用、验证结果和未结束 shell 的 execution id。下一代摘要会同时读取上一代摘要和新的旧历史，形成滚动 checkpoint。

模型收到的形式是 system block：

```xml
<session-context-compaction>
generation=3; source_ref=...
...
</session-context-compaction>
```

它是模型工作上下文，不是用户陈述。

## 两层压缩

1. 已提交的 session 历史生成持久 ledger checkpoint。`session_compactions` 只追加，Session 有独立的 compaction cursor。
2. 当前 ReAct 内已闭合的 tool batch 仍可进一步临时压缩。这一层 generation 为 0，只是当前 turn 的 ephemeral projection，不写 ledger，也不写 assistant message metadata。

## 与 Akasha 的边界

Context compaction 解决“当前 session 放不下”；Akasha 解决长期记忆。两者都从原始 transcript 出发，Akasha 不把 compaction summary 当成长期记忆原料，避免反复摘要累积误差。

## 不变量

- 数据库保真，prompt 可有损。
- 只在完整交互和完整 tool batch 边界压缩。
- 持久 checkpoint 只引用 SessionDB 中已提交的 source message id。
- 临时 active-turn projection 不能消耗 ledger generation。
- 压缩后仍超过 soft 或 hard boundary 时明确失败，不静默丢历史。
