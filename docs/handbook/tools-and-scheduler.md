# 工具、Shell、子 Agent 与 Scheduler

## ToolMeta 与 deferred 工具

每个工具除了 JSON schema，还带有 `risk`、`always_on`、`preloadable`、`search_hint` 和来源
（builtin/MCP/plugin）。核心工具默认可见；较重的 MCP/plugin 工具可以 deferred。

模型通过 `tool_search(query="select:工具名")` 解锁 deferred 工具。每个 Session 只保留有限 LRU，
避免工具 schema 长期占满上下文。

## Shell

`bash` 使用统一执行管理器：

- 前台或后台；
- PTY 与普通 pipe；
- `shell/login` 选项；
- 首次 yield 和输出 token 上限；
- timeout、取消和进程组清理。

后台执行返回 `execution_id`。使用：

- `write_stdin`：向 PTY/进程输入字符并获取新输出；
- `task_output`：兼容方式读取增量输出；
- `task_stop`：停止并清理执行。

执行归属于当前 turn 或子 Agent owner。清理一个 owner 不能误杀其他 Session 的任务。Runtime 关闭时
会清理所有剩余进程组。

## 子 Agent

`spawn` 支持 inline 和 background。每个 child 使用独立 Session 与执行 owner；inline/background
共用全局容量上限。完成后返回结构化状态并释放 Shell/MCP 等资源。

权限 profile 控制 child 可用工具。新注册工具不会自动绕过 profile；高风险工具仍需显式开放。

## Scheduler

`schedule(text, trigger, when, action="notify", context="none", timezone="", name="")`：

- `action=notify`：原样发送 text，不调用模型。
- `action=run`：在创建任务的会话中执行 Agent 工具链，自动投递最终回复。
- `context=none`：只使用任务自身输入，不加载或压缩整个聊天历史；默认值。
- `context=session`：允许读取创建会话的历史。
- `trigger=at/after/every`：ISO/HH:MM、30s/5m/2h、interval 或 5/6 段 cron；时区使用 IANA 名称，默认创建会话时区或 UTC。

例如在当前会话建立提醒：

```json
{"text":"检查训练结果","action":"notify","trigger":"after","when":"30m"}
```

工具不接受 channel/chat_id/session 覆盖。`list_schedules`、`cancel_schedule` 只能管理当前会话的任务。
最多同时保留 10 个 pending 任务。离线期间的 recurring 任务保持相位、跳过积压；one-shot 超过
300 秒宽限期后标为 missed。已有 pending fire 在尚未进入会话时可以恢复执行。

每次触发有稳定 fire key；原消息保存在原会话并标记 `scheduler:<fire-key>`。原文可查，任务文本与
回复不进入用户记忆学习，也不算作用户活跃。通知与 run 的完成都等待本地渠道回调的成功回执。
回调可能已发送后报错，或者进程在 running/delivering 时退出，会留下 needs_reconciliation，重启
不会自动重跑或重发；本地成功回执不等于收件人已读或外部渠道 exactly-once。

任务等待同一个 admission。当前还没有工具边界的自动让出与持久程序续跑；中断后记录已知结果，
未知副作用保留待核对。旧 instant/soft 文件由 Yoyo 一次迁移，迁移前保存原始字节备份；运行期只认 v2。

## 常见问题

- 后台命令找不到：使用同一个 `execution_id`，不要把 shell PID 当 execution id。
- PTY 没响应：通过 `write_stdin` 发送换行或控制字符，并检查 `process_status`。
- child 结束后进程还在：这是 owner cleanup 回归，应检查统一执行管理器，而不是再加一套 kill 逻辑。
- 任务待核对：查看 include_finished 列表、原会话消息与 execution 日志，确认外部结果后再决定是否新建任务。
