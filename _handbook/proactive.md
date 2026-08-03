# 主动推送合同

Proactive 是与被动 `AgentLoop` 并列的后台链：电量模型决定多久检查一次，Gate 决定本轮能否行动，
模块 DAG 处理 fetch/ingest/decide/deliver；没有推送时可以进入 Drift。

```text
tick → Gate → Fetch → Ingest → alert/content/context 决策 → Deliver/Drift → commit
```

## 三通道

| kind | 用途 | 触发推送 | ACK/反馈 |
| --- | --- | --- | --- |
| alert | 告警、提醒 | 优先推送 | 成功投递后 ACK |
| content | RSS、新闻、候选内容 | LLM 判断后推送或跳过 | 本地摄入后 ACK；引用内容回传 interesting |
| context | 环境与用户状态 | 只辅助判断 | 通常不 ACK |

`item_id=<source>:<event_id>` 稳定去重。投递只有在真实 Channel callback 成功后才提交 Session、
consume 与 delivery id；失败保持可重试状态。pending ACK/feedback 跨 tick 重试。FileInboxSource 将
feedback 追加到 `<id>.feedback.jsonl`，其他源可实现兼容的 feedback-aware `ack`。

## 调度与隔离

energy 只控制检查间隔，不直接提高发送概率。被动 turn 忙时 Gate 避让。content 有冷却和最大龄期；
alert 不因普通 content 冷却被吞掉。每个 tick 锁定插件代际和能力快照，在途执行不会被热重载抽走
工具或数据源。

## 判断失败

- alert 判断失败：可退回原始告警文本，不能静默丢告警。
- content 判断/解析失败：安全跳过，避免误打扰。
- source fetch 失败：进入 step/tick error 轨迹；不能把异常伪装成“今天没有内容”。
- delivery 失败：不消费对应事件。

## 运行轨迹

`proactive.db` 保存：

- `decisions`：业务结论；
- `tick_log`：每轮开始/完成、状态、terminal、总耗时与错误；
- `tick_step_log`：每个 module 的状态、耗时、terminal 与错误；
- pending acknowledgements 与 source feedback。

`ProactiveLoop.status()` 返回 unread、energy、冷却、最近 decisions 与 recent ticks。普通 Agent
轨迹另存为 session `tool_chain/context_trace` 和控制面事件。当前已具备可视化所需数据；新的前端
轨迹页属于暂缓的前端范围。

## 手动验证

```bash
python -m kirakira_agent --proactive
uv run pytest -q tests/test_proactive.py tests/test_proactive_lifecycle.py
```

完整模块拓扑与提交边界见 [docs/PROACTIVE_ARCHITECTURE.md](../docs/PROACTIVE_ARCHITECTURE.md)。
