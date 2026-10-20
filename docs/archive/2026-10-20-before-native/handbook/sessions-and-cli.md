# Session、CLI 与 TUI

Kirakira 的 Textual TUI 和 `--plain` 客户端使用同一个 Runtime。界面只提交消息、消费 lifecycle
事件，不直接调用模型，也不绕过 Session、工具或插件。

## 启动

```bash
python -m kirakira_agent --tui
python -m kirakira_agent --plain
python -m kirakira_agent --session research
```

- 不传 `--session`：创建一个新的本地会话。
- `--session research`：创建或恢复 `cli:research`。
- `/sessions`：打开已有会话选择器。
- `/session <name>`：切换或创建命名会话。
- `/clear` 或 `Ctrl+L`：只清屏，不删除 Session 或 Memory。

## Session 真相源

`<workspace>/sessions.db` 是 Session 和消息的权威存储。每条消息有稳定 id 和单调 seq；正常保存只
允许追加。旧 `sessions/*.json` 只用于一次性导入或可读镜像，不能恢复覆盖 SQLite 当前状态。

模型历史按完整 logical unit 重建，包括 assistant tool calls、tool results 和 reasoning。新版上下文压缩使用独立 ledger；旧消息中的 `react_compaction` 只做兼容回放。界面上的工具摘要只是展示，不能代替模型历史。

## Streaming 终态

```text
TurnStarted
  → ContextPrepared（可能多次）
  → StreamDeltaReady / ToolCallStarted / ToolCallCompleted
  → ContextBudgetUpdated
  → TurnFinished
```

`TurnFinished.outbound` 是唯一权威终态。TUI 用它替换 draft，不能把最终全文再追加一次。Plain
客户端只补打最终文本相对最后一个流式前缀的 suffix。

工具过程和最终回答分开显示：工具有 running/success/error/denied 状态；完整工具链、上下文计划和
usage 仍以 Session 中的 `tool_chain/context_trace` 为准。

## 中断和退出

| 操作 | 行为 |
| --- | --- |
| `Ctrl+C` | turn 运行时中断；空闲时退出 |
| `Ctrl+Q` | 退出 TUI |
| `Ctrl+L` | 清空视图，不删历史 |
| `↑/↓` | 浏览本进程输入历史；在 Session Picker 中移动 |

中断会写入结构化终态和续跑信息；不能只在界面上停止渲染而让后台 turn 继续运行。

## tmux

tmux 只负责终端断线后的进程保活，不参与 UI、Session 或 streaming：

```bash
tmux new-session -d -s kirakira-cli 'python -m kirakira_agent --tui'
tmux attach -t kirakira-cli
```

## 排查

- 会话看不到：检查 workspace 是否一致，再用 `/sessions`，不要按旧 JSON 文件名猜。
- 流式内容重复：确认客户端以 `TurnFinished` 替换 draft，而不是追加。
- 不同会话串线：检查事件是否按 `session_key` 过滤。
- 历史突然减少：这是严重错误；正常 context retry 只能改变模型投影，不能改变 `sessions.db`。
