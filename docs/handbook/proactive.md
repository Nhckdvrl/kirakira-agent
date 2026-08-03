# 使用主动执行

Proactive 是与被动 Agent turn 并列的后台链。energy 模型决定检查间隔，Gate 判断本轮是否适合行动，
模块 DAG 完成抓取、摄入、判断、投递；没有投递时可以进入 Drift。

```text
tick → Gate → Fetch → Ingest → Decide → Deliver 或 Drift → Commit
```

## 三类事件

| kind | 用途 | 处理方式 |
| --- | --- | --- |
| `alert` | 提醒、告警 | 优先判断；判断失败时可退回原文，不能静默丢失 |
| `content` | 新闻、RSS、候选内容 | 模型判断推送或跳过；摄入后 ACK |
| `context` | 环境和用户状态 | 只辅助判断，通常不 ACK |

事件使用稳定的 `item_id=<source>:<event_id>` 去重。只有 Channel callback 确认成功后，系统才提交
Session、consume 和 delivery 状态。失败的投递保持可重试；ACK 与 feedback 也会跨 tick 重试。

## 配置与启动

主动执行配置位于 `config.toml` 的 `[proactive]` 及其子段。常用开关包括 energy、content 冷却、
最大内容龄期和 `[proactive.drift]`。

```bash
uv run python -m kirakira_agent --proactive
```

被动 turn 忙时 Gate 会避让。energy 只决定多久检查一次，不直接提高发送概率。alert 不受普通
content 冷却影响。

## 数据源与反馈

内置 File Inbox 从 `proactive/inbox/<source>.jsonl` 读取。插件也可以声明兼容的
`ProactiveSource`。content 被引用时会回传 `interesting` feedback，未引用时可回传相应跳过结果；
文件源把反馈写入 `<source>.feedback.jsonl`。

## 运行轨迹

`proactive.db` 保存：

- `decisions`：每个候选的业务结论；
- `tick_log`：每轮开始、完成、terminal、耗时和错误；
- `tick_step_log`：每个模块的状态、耗时和错误；
- pending ACK、feedback 和 delivery 去重状态。

`ProactiveLoop.status()` 返回 unread、energy、冷却、最近 decision 和 recent tick。数据层已经支持
轨迹可视化；前端页面不在当前后端范围内。

## 验证

```bash
uv run pytest -q tests/test_proactive.py tests/test_proactive_lifecycle.py
```

完整拓扑和提交边界见[主动执行架构](../architecture/proactive.md)。Drift 的使用方式见
[Drift 手册](./drift.md)。
