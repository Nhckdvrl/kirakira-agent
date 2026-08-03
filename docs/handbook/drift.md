# 使用 Drift 空闲任务

Drift 是 Proactive 没有产生推送时可以运行的后台 agent task。任务内容不写死在 runtime，而由
`<workspace>/drift/skills/<name>/SKILL.md` 定义。

```text
无主动推送
  → 检查开关、hazard 到期和最小间隔
  → 选择一个 skill
  → 注入记忆、近期上下文和上轮连续性
  → 复用同步 Agent 与默认工具执行
  → 可选推送草稿
  → 保存 run、journal 和连续性
```

## Skill 格式

```markdown
---
name: review-memory
description: 抽查一条长期记忆是否仍然准确
---

## 目标

## 工作流程

## 要求
```

首次初始化会提供 `explore-curiosity` 和 `review-memory` 示例。每轮会重新选择 skill，当前策略优先
选择最久没有运行的任务。

## 一轮怎样运行

- system prompt：选中的 `SKILL.md`；
- 首条消息：Drift Briefing，包含记忆、近期上下文、journal 和 continuation；
- 工具：默认工具，加 `message_push` 与 `finish_drift`；
- 持久化：`drift/drift.db`。

`message_push(message)` 只生成本轮草稿，runner 回到主事件循环后才通过 Channel 投递；每轮最多一次。
`finish_drift(status, briefing, scratchpad_update?, next_tendency?)` 用于明确收尾：

- `completed`：本轮闭环完成；
- `paused`：尚未完成，必须写 `scratchpad_update` 作为下次断点。

达到 `max_steps` 仍未收尾时按 `paused` 记录。只有 Channel callback 成功才把结果记为 `sent` 并写入
Session；没有草稿或发送失败都记为 `silent`。

## 触发与连续性

Drift 同时受 `min_interval_hours` 和 hazard schedule 控制。用户再次发言或上轮 Drift 完成后，
timer anchor 会变化并重新采样下一次到期时间。采样结果落盘，重启不会把到期时间随意后移。

每个 skill 的 continuation 保存：

- `scratchpad`：暂停时的续跑位置；
- `next_tendency`：下一轮可能继续的方向，不是硬指令；
- journal 与 self-observation：帮助跨轮减少重复。

## 开关与排查

配置位于 `config.toml` 的 `[proactive.drift]`。`enabled=false` 时不构造 DriftRunner。Drift 消息会带
`metadata.proactive=true` 和 `metadata.drift=true`。

```bash
uv run pytest -q tests/test_drift.py
```

若没有运行，依次检查：开关、skill 是否存在、最小间隔、hazard 到期时间，以及本轮 Proactive 是否
已经投递消息。
