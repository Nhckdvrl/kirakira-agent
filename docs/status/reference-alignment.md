# Reference 对齐状态

审计日期：2026-10-02。原对齐点 `ac6f7652` 是 2026-08-15 的历史记录。
第一轮对照 Akashic `0be132326ee6c410d1b2eaa85c09e1b50dcc5a9e`，第二轮更新至 `7cf10136932d5fe0205727c803c00861272db0b8`，不能再用“最新已对齐”概括全项目。

## 对齐口径

对齐是同一职责有明确 owner、关键行为有合同与证据，且运行不依赖本地 `Reference/`。
不要求相同目录/行数，不意味着继承上游未验证的质量结论。详细源码比较见[审计报告](../engineering/akashic-review-2026-10-02.md)。

| 领域 | Kirakira 当前落点 | 与本次上游 pin 的关系 |
| --- | --- | --- |
| 执行内核 | DefaultReasoner + scoped 调用 + ReasoningPolicy | 共用循环与授权原则；不是上游 OrderedReact 消息程序系统 |
| 主动决策 | share/skip 工具、候选身份检查、持久检查/重试、版本结算与过期判断 | 对齐决策边界；没有完整 Eventmail 和质量衰减池 |
| Drift | 共享异步执行、收窄工具、finish 后提交 | 保留旧 Skill 产品语义；尚非最新持久 proposal 服务 |
| Akasha | v1 数学 + 原子提交收据/消费位置 + 同 cycle 重建 + cutover 后补消费 | 对齐部分可恢复性原则；不是 v2 算法或 scope 路由 |
| Session/Context | 原始消息、compaction ledger、预算投影 | 既有合同保留；不宣称完整上游消息绑定/归档模型 |
| Plugin/MCP | 代际、快照、准入、热更新、owner cleanup | 既有实现；未移植当前 plugin_composition 内核 |
| Workspace/工具/调度 | 锁、Yoyo、Shell/PTY、子任务、cron | 本轮保留的基础能力，不能凭功能同名推断完全 parity |
| 产品/质量 | 当前 TUI/Web/dashboard 与离线合同测试 | 移动产品面、质量 benchmark、完整运维体系不在本轮 |

延期事项的唯一维护位置是[当前状态](./current.md)。

## 独立性

生产代码不得 import 或读取 Reference。测试、构建、CLI 在没有 Reference 的隔离源码副本中执行。
固定上游 commit 只是来源文本，运行不能从审计 checkout 导入代码。

```bash
uv run pytest -q
uv build
uv run python -m kirakira_agent --help
```
