# CLI、TUI 与本地 Session

## 谁负责什么

全屏界面是项目自己的 Textual client，不是 tmux UI。tmux 只负责让进程在终端断开后继续运行：

```text
tmux（可选保活）
  └─ Kirakira Textual TUI
       └─ MessageBus / lifecycle events / Runtime
```

`--plain` 使用同一 Runtime 和 lifecycle reducer，只把事件按行打印，适合日志、SSH 弱终端和管道。
两种界面都不是另一套 Agent，不会绕过 Session、Context、工具或插件链路。

## Session 规则

- 不传 `--session`：每次启动生成 `chat-YYYYMMDD-HHMMSS-<随机>`，初始为空。
- 传 `--session research`：创建或恢复 `cli:research`。
- `/sessions`：打开按更新时间排序的历史选择器；`↑/↓` 选择，Enter 恢复，Esc 返回。
- `/session <name>`：直接切换或创建命名 Session。
- `/clear` / `Ctrl+L`：只清屏，不删除 Session，也不清 Memory。
- Session 在第一轮 commit 后保存在 `<workspace>/sessions/`；JSON 是事实源，SQLite FTS 是索引。

切换 Session 时，界面从 JSON 恢复 user/assistant 内容和工具名摘要；新 turn 的 history reconstruction
仍由 Session 层重新展开 tool calls/results/reasoning，不能用屏幕上的摘要代替模型历史。

## Streaming 的权威终态

生命周期顺序大致是：

```text
TurnStarted
  → ContextPrepared（可能多次）
  → StreamDeltaReady / ToolCallStarted / ToolCallCompleted（多轮）
  → ContextBudgetUpdated
  → TurnFinished（唯一权威终态）
```

TUI 用 `TurnViewState` 将增量归并到当前 iteration。`TurnFinished.outbound` **替换** draft，绝不把
最终全文再追加一次；这是避免流式内容变成 `hellohello` 的核心合同。Plain CLI 也只打印最终文本相对
最后一轮已流式前缀的 suffix。如果模型把很短的回答放在一个 SSE chunk 中，视觉上会像一次性出现，
不代表 Runtime 绕过了 streaming。

工具调用期间，上一 iteration 的说明会保留为 `note`，当前工具显示 running/success/error/denied；
最终 answer 与过程 trace 分开渲染。`ContextPrepared` 显示计划、估算/预算和 history 数，完整明细仍以
Session `context_trace` 为准。

## 快捷键

| 输入 | 行为 |
| --- | --- |
| `Enter` | 发送当前输入 |
| `↑` / `↓` | 浏览本次进程的输入历史；Session Picker 中用于选择 |
| `Ctrl+C` | turn 运行时中断；空闲时退出 |
| `Ctrl+L` | 清空当前视图，不删历史 |
| `Ctrl+Q` | 退出 |
| `/sessions` | 打开持久化 Session 选择器 |
| `/session <name>` | 切换/创建命名 Session |

## tmux 运行

```bash
tmux new-session -d -s kirakira-cli 'python -m kirakira_agent --tui'
tmux attach -t kirakira-cli
```

退出 TUI 后，若该 pane 只运行这一条命令，tmux Session 随之结束，这是 tmux 的正常行为。重新启动
TUI 默认会得到新空 Session；需要继续旧对话时用 `/sessions` 或 `--session <name>`。

## 不变量

- UI 只消费事件和提交 InboundMessage，不直接调用模型。
- tmux 不参与绘制、Session 或 streaming。
- Draft 可以变化，`TurnFinished` 才是最终答案。
- `/clear` 永远不是删除；删除必须走明确的 Session 管理操作。
- 不同 Session 的 lifecycle event 必须由 `session_key` 过滤，不能串到当前界面。
