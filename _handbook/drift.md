# Drift 空闲任务

## 先理解它是什么

Drift 是**你写模型可以做什么、模型照着执行**的后台任务系统。

- **什么时候跑**：[主动链路](./proactive.md)完成一轮但没有产生推送，例如没有候选、content 被 skip、
  正在冷却或只有 context。被动链路忙导致的 gated tick 不会进入 Drift。
- **做什么**：你写在 `<workspace>/drift/skills/<name>/SKILL.md` 里的事。
- **怎么做**：SKILL.md 是一份分步操作指南，作为 system prompt，模型一步步按着走，最后调 `finish_drift` 收尾。
- **跟主动推送的本质区别**：主动推送的行为是**代码里写死的 system prompt**；Drift 的行为是**你写的 SKILL.md**——可编辑、可增删，不改代码。

这是 Kirakira 的第二条差异化链路。参考 akashic 的 `plugins/drift_flow`；本项目 MVP
**复用同步 Agent 与默认工具集**把"一轮 Drift"跑成一次 agent run，刻意不搬 hazard 穿线 /
self_observation journal 等 Tier-3 细节。代码在 `kirakira_agent/drift/`。

```text
主动链路本轮没有产生推送
  └─ DriftRunner.maybe_run(now, session_key):
     ├─ enabled？min_interval 到了？有 skill 吗？   （任一不满足 → 不跑）
     ├─ 每轮重新选一个 skill（最久没跑过的优先）
     ├─ 拼 Drift Briefing（记忆 + 近期上下文 + 本 skill 连续性 + 最近 run）
     ├─ 复用 Agent loop 跑一次 run：SKILL.md=system prompt，Briefing=首条消息
     │   工具集 = 现有工具 + message_push（记草稿）+ finish_drift（收尾）
     ├─ run 结束 → 若有草稿消息，在主循环上投递
     └─ 落库：drift.db 记 run + 连续性
```

## 一轮 Drift = 一次 agent run

这是理解 Drift 的关键：它不是一套独立的执行引擎，而是**借用被动链路那套 Agent**，
只是换了 system prompt 和工具集：

- **system prompt** = 选中 skill 的 `SKILL.md` 正文。
- **首条消息** = runtime 注入的 Drift Briefing。
- **工具集** = `build_default_registry` 的现有工具（read/write/bash/recall/fetch_messages/web...）
  外加两个收尾工具。跑完调 `finish_drift` 结束。

> **注意**：内置 `message_push` 是 async 且直连 bus。Drift run 跑在工作线程里
> （`asyncio.to_thread`），在里面跨事件循环访问 bus 不安全。所以 `register_drift_tools`
> 先 `unregister` 内置版，再注册一个**同步**版：只把消息记成草稿，真正投递由 runner 在
> run 结束后回到主事件循环上完成。这是 MVP 的一处关键实现取舍。

## SKILL.md 格式

```markdown
---
name: <skill-name>
description: <一句话描述>
---

## 目标
## 工作流程
1. ...
2. ...
## 要求
- 约束和规则
```

放在 `<workspace>/drift/skills/<name>/SKILL.md`。首次运行会自动放两个可执行示例：
`explore-curiosity`（会推送：像朋友随口一问，演示 message_push 路径）和 `review-memory`
（纯后台：抽查一条长期记忆是否仍准确，演示静默收尾与跨轮连续性）。

| 文件 | 谁维护 | 说明 |
| --- | --- | --- |
| `drift/skills/<name>/SKILL.md` | **你写** | 任务定义，agent 每轮当 system prompt 读 |
| `drift/drift.db` | **runtime 写** | run 记录、跨轮连续性、min_interval 门控 |
| `drift/skills/<name>/*` | 按需 | skill 自己的工作文件 |

## 收尾工具

| 工具 | 用途 |
| --- | --- |
| `message_push(message)` | 生成一条待发草稿（本轮最多一次）；runner 回主循环后等待 Channel 确认 |
| `finish_drift(status, briefing, scratchpad_update?, next_tendency?)` | 保存状态并结束本轮，执行结束前必须调用 |

`finish_drift.status`：
- `completed` — 本轮小闭环已完成，不强行编造下一步。
- `paused` — 本轮没做完，**必须**写 `scratchpad_update` 说明下次从哪继续。

`message_result` 由 runtime 记录：Channel callback 成功是 `sent`；没有草稿或发送失败都是
`silent`。只有 `sent` 才写 Session。这对齐 Reference `record_commit_result(ctx, sent)`，不由 skill
自报，也不另外引入草稿重发队列。

## 核心约束

1. **每次重新选择**：不默认继续上次的 skill，每轮重新比较（当前实现：最久没跑过的优先）。
2. **message_push 是 fire-and-forget**：最多一次；不保存"等待回答"，也不推断"用户没回"。
   用户以后真回答时，由会话和记忆链路自然关联。
3. **必须 finish_drift**：run 到达 `max_steps` 仍没收尾时，本轮按 `paused` 记录。
4. **最小间隔**：`min_interval_hours` 控制连续两次 Drift 的最小间隔（`drift.db` 的 last run 判定）。

## 跨轮连续性

Drift 会被反复触发，既要保留当前意图，也要能从多轮里形成暂定认识。MVP 保存在
`drift.db` 的 `continuum` 表（每个 skill 一行）：

- `scratchpad` — `paused` 时写的续跑断点，下轮通过 Briefing 注入。
- `next_tendency` — 下次可能想做什么的宽松倾向（不是硬指令）。

Briefing 还会带最近 5 条 run 记录，让 skill 避免短期重复。

> 参考 akashic 还有 self_observation journal（question/reinforce/revise 三态观察）和 hazard
> 到期采样，属于 Tier-3，MVP 暂未搬，接口留在 `state.py` 的连续性表上，日后可扩。

## 配置与开关

见 `config.toml` 的 `[proactive.drift]` 段。`enabled=false`（默认）时 `DriftRunner` 不构造；
建议先跑通主动推送再开启。Drift 投递的消息打 `metadata.proactive=true, drift=true`。

## 验证清单

```text
┌─ enabled=false → maybe_run 直接返回 False
├─ 无 skill → 不跑（但会先落一个示例 skill）
├─ min_interval 未到 → 不跑
├─ 一轮 run：message_push 记草稿 → finish_drift → runner 投递 + 落库
└─ Channel 失败：message_result=silent，不写 Session
```

对应测试：`tests/test_drift.py`。
