# 启动层与多渠道合同

本文只描述已经接入运行时且可以验收的启动与 Channel 行为。实现顺序与边界优先参考本地
`Reference/`：`main.py`、`agent/supervisor.py`、`bootstrap/setup_wizard.py`、
`infra/channels/telegram_channel.py`、`infra/channels/qq_channel.py`。

## 1. 入口

| 命令 | 行为 |
| --- | --- |
| `uv run python main.py` | 无配置时进入 setup；有配置时由 supervisor 托管 gateway |
| `uv run python main.py setup` | 交互配置模型和全部渠道，并初始化 workspace |
| `uv run python main.py init` | 非交互复制模板并初始化 workspace |
| `uv run python main.py gateway` | 绕开 supervisor，直接启动完整服务，供调试 |
| `uv run python main.py supervise` | 显式进入与默认入口相同的 supervisor |
| `uv run python main.py control <子命令>` | **连接已在跑的 agent**,不启动 runtime;见第 5 节 |

`uv.lock` 固定 Python 依赖；`uv run` 首次运行会创建隔离环境，不要求用户手工创建 `.venv`。

## 2. 默认进程链

```text
main.py
  → entry：解析 config/workspace，必要时 setup
  → supervisor：取得 <workspace>/.supervisor.lock
      → 创建 boot_id、nonce、私有 commit pipe
      → 启动 main.py gateway
          → 构建 Agent/Bus/Channel/Proactive/Drift
          → 所有已配置 Channel start 成功
          → 控制面在 <workspace>/.kirakira/control.sock 上监听(0600)
          → 原子写 .runtime-ready.json（bootId + pid + ready）
      → SIGINT/SIGTERM 精确转发给当前 child
      → 普通退出：supervisor 同步退出
      → exit 75：只有收到当前 boot 的合法 restart_commit 才换代
```

任何已启用 Channel 的凭据校验或连接失败都会让 gateway 启动失败，不会发布 readiness。当前已经有
supervisor 的安全接收端，但还没有 Reference 的 `agent_restart` 工具准入协调器，因此 Agent 本身不会
主动请求换代。

## 3. Setup 的渠道流程

### 3.1 Web

Web 无外部凭据，默认写入：

```toml
[channels.chat]
enabled = true
host = "127.0.0.1"
port = 6322
channel_name = "web"
```

它随 gateway 启动；被动请求走 `/message`，主动消息由 `/events?session_id=...` 长轮询领取。

### 3.2 Telegram

向导顺序与 Reference 一致：

1. 输入 BotFather token，调用 `getMe` 验证。
2. 输入允许访问的 Telegram user id 或 username。
3. 若开启 Proactive，用户向 bot 发一条新消息。
4. 向导调用 `getUpdates` 匹配 id/username，取得 `chat_id` 并确认消费该 update。
5. 写入 Telegram Channel 与 `proactive.target=telegram/chat_id`。

运行时直接移植 Reference 的 `python-telegram-bot` Channel 和 `telegram_utils`：覆盖文本、图片、文档、
被回复文本/附件、白名单、消息去重、`/stop`、typing、工具/思考/回复实时预览、429 `retry_after`、
Conflict 停收、UTF-16 长消息切分、Markdown entities 和图片/文档出站。`infra/channels/` 下的
`base.py`、`contract.py`、`reply_context.py`、`telegram_channel.py`、`telegram_utils.py` 与固定 Reference
源码逐字节一致；namespace、MessageBus、SessionManager、message-push 和 interrupt 差异全部位于文件外
的 compatibility/binding 层。
启动时注册 bot commands；注册或轮询失败会阻止服务被标记为 ready。

### 3.3 QQ / NapCat / OneBot

这是 `[channels.qq]`，不是腾讯开放平台官方 QQBot。向导收集 bot QQ、OneBot HTTP API、access token、
私聊白名单与群白名单，并调用 `get_status` 验证 API。NapCat HTTP 事件上报地址是：

```text
http://127.0.0.1:8766/qq/webhook
```

私聊 session 为 `qq:<user_id>`；群聊 chat id 为 `gqq:<group_id>`。群消息支持逐群白名单与
`require_at`，并覆盖 CQ 图片入站、私聊/群聊文本和媒体出站、`/stop`、事件去重与 OneBot retcode
失败传播。主动目标可直接使用 `channel="qq"`、`chat_id="用户QQ号"`。

### 3.4 腾讯开放平台官方 QQBot

这是独立的 `[channels.qqbot]`。Reference 通过外置插件提供运行时，插件源码不在本地基准仓库；其
setup 协议完整存在。Kirakira 将这份协议内置实现：

1. 用 AppID/AppSecret 调 `getAppAccessToken` 验证凭据。
2. 请求官方 Gateway URL，建立 WebSocket。
3. 收到 Hello 后发送 Identify，intent 为 C2C。
4. 监听第一条 `C2C_MESSAGE_CREATE`，自动取得 `user_openid`。
5. 写入白名单；主动目标使用 `channel="qqbot"`、`chat_id="c2c:USER_OPENID"`。

运行时负责 token 提前续期、Gateway 心跳、断线重连、C2C 白名单和去重；被动回复带原始 `msg_id`，
主动消息通过 `/v2/users/{openid}/messages` 发送。Gateway 在启动超时内没有完成 Identify 时启动失败。

## 4. 统一数据流

```text
Web / Telegram / QQ / QQBot
  → Channel 校验身份、去重、下载附件
  → InboundMessage(channel, chat_id, sender, media, metadata)
  → MessageBus → AgentLoop → PassiveTurnPipeline
  → OutboundMessage(channel, chat_id, content, media)
  → MessageBus 根据 channel 回到原 Channel

Proactive / Drift
  → OutboundMessage(target.channel, target.chat_id, proactive=true)
  → 同一个 MessageBus 和同一个真实 Channel sender
  → sender 成功返回后才提交 delivery_id、Session、consume/ACK
```

因此“配置成功”的验收标准不是 TOML 能解析，而是：Channel 能在 gateway 启动阶段真实就绪、被动消息
可往返、主动消息完成 sender callback，失败时不提交主动状态。

## 5. 控制面入口

agent 跑起来之后,`main.py control` 从**另一个终端**连上它的私有 socket。
它不启动 runtime,也不要求 `config.toml` 存在——只需要 workspace 指对。

```text
uv run python main.py control status                 看 ready / workspace
uv run python main.py control threads --limit 10     列会话
uv run python main.py control new --ask "你好"        新建 thread 并跑一轮
uv run python main.py control ask <threadId> "..."   在已有 thread 上继续
uv run python main.py control read <threadId> --turns 看这个 thread 的历史 turn
uv run python main.py control interrupt <threadId> <turnId>
uv run python main.py control consolidate <threadId> 强制归档记忆
uv run python main.py control plugin-drain <pluginId>
```

控制面 turn 走 `programmatic:<uuid>` 命名空间,**不会与渠道会话串台,也不产生
渠道出站消息**。协议细节、状态机与认证见
[design/control-plane.md](./design/control-plane.md)。

环境变量:

| 变量 | 作用 |
| --- | --- |
| `KIRAKIRA_CONTROL_ENDPOINT` | 改监听/连接地址;支持 loopback TCP(`127.0.0.1:9800`) |
| `KIRAKIRA_CONTROL_TOKEN` | 配置后 `initialize` 必须带匹配的 `workspaceToken` |
