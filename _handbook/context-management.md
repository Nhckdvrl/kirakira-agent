# 上下文管理

## 一轮消息怎样进入模型

上下文不是一段不断增长的字符串，而是有名字、有优先级、可观测的 block：

```text
稳定 system
  identity → behavior_rules → skills_catalog → self_model
  → long_term_memory → session_context

逐轮 Context Frame（role=user，但带系统标记）
  recent_context → active_skills → retrieved_memory
  → turn_injection → plugin_hints

历史消息 → Context Frame → 当前用户原文
```

动态内容放在带 `data-system-context-frame="true"` 标记的 reminder 里，位置固定在历史之后、
当前消息之前。它明确声明这些内容不是用户陈述，避免模型把检索记忆或插件提示当成用户原话。
`identity`、`behavior_rules`、`skills_catalog` 等稳定 block 按 workspace + 内容签名缓存。

Skill 默认只把目录放进 `skills_catalog`；用户用 `$skill-name` 点名后，正文进入
`active_skills`。需要每轮加载的 skill 可在 `SKILL.md` frontmatter 写 `always: true`。

## 预算与降级

输入预算为：

```text
floor(context_window × effective_context_percent) - max_tokens
```

估算覆盖 system、所有消息字段、工具 schema 和图片块。Runtime 在每个 ReAct step 记录估算，
OpenAI-compatible Provider 在发网络请求之前再做一次最终预检，防止漏算工具解锁后的 schema。
当前估算器有意采用保守的 `text chars / 3 + image allowance`，不是模型 tokenizer；真正计费值以
Provider 返回的 `usage` 为准。估算用于提前拒绝明显超限和比较 retry plan，不应被当作账单数字。

超限不是直接截字符串，而是重新 render：

1. `full`
2. `trim_skills_catalog`
3. `trim_recent_context`
4. `trim_long_term_memory`
5. `trim_retrieved_memory`
6. 保留上述裁剪并把历史缩到 50%
7. 保留上述裁剪并移除历史

每次历史切片都会回退到 user 边界，绝不从半组 tool call 开始。工具结果超过上限时保留头尾，
并写明总行数与省略字符数，避免只保留开头而丢掉末尾错误。

## Consolidation 边界

Session JSON 保存完整对话；送入模型的历史从 `last_consolidated` 开始。未归档区达到
`history_window + max(5, history_window / 2)` 时，本轮开始前必须先推进 consolidation。
如果归档游标没有前进，本轮会明确失败，而不是悄悄丢掉旧消息。

## 怎么检查一轮上下文

每条 assistant session message 的 `context_trace` 包含：

- 所有 attempt 的计划名、history window、disabled sections；
- 每个 block 的 chars、估算 tokens、static/cache hit；
- Provider 返回的 prompt/completion/total token usage；
- 最终选中的计划与 ReAct request 数。

Session 顶层 metadata 的 `context_budget` 保存回复提交后的 history token baseline，供下一轮
和诊断工具读取。TUI/Plain CLI 会显示当前计划、估算/预算与历史规模。

直接检查持久化数据时，找到 `<workspace>/sessions/*.json` 中对应 session：assistant message 上的
`context_trace` 是本轮明细，根级 `metadata.context_budget` 是下一轮基线。Session 文件名带 key hash，
不要根据文件名猜；TUI 的 `/sessions` 或 SessionManager 索引才是正式查找入口。

## 当前重试边界

语义裁剪发生在 `run_turn()` 外层，因此 ContextLengthError 发生在工具循环中途时，会用下一计划
重新执行整轮 Reasoner。这样能保证 prompt hooks、工具可见性和消息协议重新一致，但已经成功执行的
外部副作用工具理论上可能再次被模型选择。文件原子写、调度幂等和重复调用 guard 能降低部分风险，
但通用外部工具仍应自行使用幂等 key。未来如果加入跨 attempt 的 tool-result replay，必须同时证明
不会把旧计划下的无效上下文当成新计划证据。

## 不变量

- 不把检索记忆伪装成用户原文。
- 不在超限后静默丢历史。
- 不只计算文本而漏掉工具 schema 或图片。
- 不复用上一轮渲染结果做重试；每个计划都重新经过 plugin prompt hooks。
- Context Frame 只能提供候选上下文，工具结果仍是外部事实的证据边界。
