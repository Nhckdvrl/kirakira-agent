# 启动与 Channel 架构

Kirakira 的构建、测试和运行只依赖仓库内代码，不读取外部 Reference checkout。

## 启动入口

| 命令 | 行为 |
| --- | --- |
| `uv run python main.py` | 无配置时进入 setup；有配置时由 supervisor 托管 gateway |
| `uv run python main.py setup` | 交互配置模型、Channel 和 workspace |
| `uv run python main.py init` | 非交互初始化 workspace |
| `uv run python main.py gateway` | 直接启动服务，适合调试 |
| `uv run python main.py supervise` | 显式启动 supervisor |
| `uv run python main.py control …` | 连接已运行的控制面，不另起 runtime |

Supervisor 负责子进程换代。受控重启使用退出码 75，由 supervisor 拉起新 generation。PID、私有控制管道
和 generation 信息属于托管协议，不应由业务模块直接操作。

## Workspace 启动顺序

```text
解析配置
  → 获取 .instance.lock
  → 执行 yoyo workspace migrations
  → 打开 Session、Memory、Control 和 Proactive 状态
  → 组装插件、工具、MCP 与 Channel
  → 启动 gateway
```

`.instance.lock` 防止同一 workspace 被两个 gateway 同时写入。迁移在状态库打开前完成；迁移失败时
runtime 不继续启动。迁移合同见[Workspace 迁移](../operations/workspace-migrations.md)。

## Channel 边界

Channel 只负责外部消息接入和投递确认。内部统一转换为 message envelope，再交给 Session、AgentLoop 或
Proactive。当前配置面包括 Web Chat、Telegram、QQ 和 QQBot，实际启用集合由 `config.toml` 决定。

投递 callback 是提交边界：

- 被动回复成功后才确认外发结果；
- Proactive/Drift 只有 callback 成功才写 Session、consume 和冷却状态；
- callback 明确失败时保留可重试状态；
- 去重采用持久化 delivery fingerprint，语义是窗口内至多一次，不宣称 exactly-once。

## 控制面

`main.py control` 通过本地 socket 连接 gateway 内的控制服务。它与具体 Channel 并列，不会为了执行
命令再构造一份 Agent runtime。详细协议见[控制面架构](./control-plane.md)。

## 关停

关停按依赖逆序执行：停止接收新请求，等待或中断在途任务，关闭 Proactive/Channel/MCP/插件，释放
数据库和 workspace lock。各资源的 close/terminate 必须幂等，单个关闭失败不能跳过后续资源。
