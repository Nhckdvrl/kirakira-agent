# Reference 对齐状态

审计基线：`ac6f7652`（2026-08-15 同步的最新 Reference main）。

## 对齐口径

这里的“对齐”指三件事：

1. 相同职责进入相同的功能 owner，不再保留两套正式实现。
2. Reference 已有的关键运行合同在 Kirakira 中存在，而且可以执行。
3. `Reference/` 可删除；运行、测试、构建和迁移不读取它。

它不表示逐文件复制或保持相同行数。Reference 的 `schema/`、`types/`、`prompts/` 等细分根，
在 Kirakira 中分别由 `core/schema.py`、owner 内类型和 `agent/prompting/` 承担。

## 已闭合的主要 gap

| 能力 | 当前实现 |
| --- | --- |
| Workspace | 单实例锁、Yoyo ledger、origin migration、append-only 检查 |
| Context | 持久历史 append-only、CommittedContextUnit、74%/hard gate、20k raw tail、滚动 summary ledger、active-turn ephemeral compaction、usage coverage |
| Model runtime | async/stream、DeepSeek reasoning/tool call、错误分类、轻模型安全 fallback |
| Tool runtime | ToolMeta、deferred search、统一 Shell/PTY/stdin、owner cleanup |
| Subagent | inline/background、独立 Session、容量、取消和完成回注 |
| Scheduler | at/after/every、cron、timezone、instant/soft、隔离 soft Session |
| Plugin/MCP | 声明式能力、代际、准入、snapshot lease、热更新和失败回滚 |
| Memory | MemoryServices、Default、Akasha v1、Markdown maintenance、证据和检索回放 |
| Proactive/Drift | 模块 DAG、三通道、ACK/feedback、delivery dedup、tick/step trace、连续性 |
| Control/observe | programmatic turn、中断、tool/context trace 和事件流 |
| Quality gate | 语义合同、change-impact、全量测试、在线 verifier |

## 有意保留的差异

- **Akasha**：使用 v1，不迁 v2 的重建、恢复和更厚事务合同。
- **产品前端**：不复刻 Reference 的 Android/mobile/共享 React 产品面。
- **运维与 benchmark**：只保留当前项目真正需要的质量门禁，不复制完整发布基础设施。
- **Provider**：当前以 OpenAI-compatible/DeepSeek 为主，没有完整 provider catalog。

## 独立性检查

生产代码不得 import 或读取 `Reference`。以下操作在 `Reference/` 不存在时必须成功：

```bash
uv run pytest -q
uv build
uv run python -m kirakira_agent --help
```

上游 commit pin 只能作为纯文本来源信息存在。移植完成后，代码必须由 Kirakira 自己的 owner 承重。
