# 验证记录

本文只记录已经实际执行过的验证。功能现状见[当前状态](../status/current.md)，不要从测试数量反推能力范围。

## 最近一次完整结果

环境：2026-08-15，本仓库当前 Python/uv 环境。

| 验证 | 结果 |
| --- | --- |
| 全量测试 | 588 passed，另有 4 个 subtests 通过 |
| 构建 | 成功 |
| CLI smoke | 成功 |
| 删除/移走 Reference 后测试、构建、CLI | 成功 |
| DeepSeek 在线验证器 | 成功 |

Reference 独立性验证证明外层 Kirakira 不 import、不读取、也不要求本地存在 `Reference/`。Reference 仅在
开发期用于人工对照；删除它后项目行为不应改变。

## DeepSeek 在线验证覆盖

通过已配置的 DeepSeek API 实际执行 `kirakira-verify-online`，覆盖：

- 普通模型响应与 provider usage；
- forced tool call 协议，参数为 `probe-ok`；
- 默认工具 `read_file` 的真实 AgentLoop 调用；
- 1024 维 embedding 写入与查询；
- 长 session 真实触发 context compaction，生成 generation 1 ledger；
- 摘要覆盖 52 条 source messages，保留 246 条 provider 投影消息，原始 message id 和顺序不变；
- 摘要的 Goal / Progress / Decisions / Next Steps / Critical Context 标题合同通过；
- context trace 中模型请求 usage coverage 为 `exact`；
- Akasha v1 摄入、检索、回源，并由模型在最终回答中消费证据。

该次 provider probe 返回 input 17、output 7 tokens。这个数字只证明遥测字段和归一化链路可用，不作为
性能基准。

## 常用命令

```bash
uv run pytest -q
uv build
uv run python main.py --help
uv run kirakira-verify-online
```

在线验证会读取本地 `config.toml` 中的 provider 和 embedding 配置，但不会打印 API key。验证器使用临时
workspace，不污染真实 Session、Memory 或 Proactive 数据。

## 结果口径

- **已测试**：合同、边界和回归由自动化测试覆盖；
- **已在线验证**：真实 provider 和真实 AgentLoop 已执行；
- **未验证**：没有把 mock 或静态检查包装成实弹结果。

## 尚未覆盖的实弹范围

- Telegram、QQ、QQBot 等外部 Channel 的真实账号端到端投递；
- 完整 LongMemEval 的模型 Judge/F1/EM 质量评测；
- Akasha 新版本的全量图行为 parity；
- 前端、移动端和轨迹可视化页面。

这些边界不影响当前后端架构完整性，但不能宣称已经做过在线端到端验收。
