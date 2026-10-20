# 工具、Shell、子 Agent 与 Scheduler

## ToolMeta 与 deferred 工具

每个工具除了 JSON schema，还带有 `risk`、`always_on`、`preloadable`、`search_hint`、`exclusive_batch` 和来源
（builtin/MCP/plugin）。核心工具默认可见；较重的 MCP/plugin 工具可以 deferred。

模型通过 `tool_search(query="select:工具名")` 解锁 deferred 工具。每个 Session 只保留有限 LRU，
避免工具 schema 长期占满上下文。

提供者可在 `registry.register(..., risk="read-only", parallel_safe=True)` 显式声明独立读取可并发。
默认并发上限为 4，通过 `AGENT_MAX_PARALLEL_TOOLS` 调整；设为 1 使用串行执行。
read_file/list_dir 已启用，其他工具默认串行；写入、tool_search 和终结工具会等待前面的读取结算。
模型下一次请求看到的工具结果仍按原调用顺序排列。取消同步工具时，会等线程实际结束再释放会话。

`exclusive_batch` 工具必须单独调用，混合批次整体拒绝。当前 `set_working_directory` 使用此合同，
避免同一批读写分散到切换前后的目录。项目和 cwd 的 Web/API 使用见[项目与会话工作目录](projects-and-directories.md)。

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

background 完成后，结果先保存到 Session 数据库中的 Jobs owner。原会话仍在运行时，下一次模型请求
会读到结果并合并回复；原会话空闲且渠道可投递时，自动唤醒并按原来源回复。结果已被当前轮领取时
不会再额外唤醒。后台回传不增加用户消息，也不作为新的用户意图学习。

`spawn_manage(action="results")` 列出当前会话的持久结果，传入 `task_id` 可读取单项结果、消费程序
和最终消息回执。`list` 查看运行中任务，`cancel` 取消本会话任务；这些工具需要先通过 tool_search
加载。结果在成功回复提交后才标记 consumed；取消或重启后的未完成程序保留领取，显式恢复时继续
使用原结果。已结束进程不会因为重启自动重新运行；运行中的子代理本身尚不支持跨重启续跑。

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
