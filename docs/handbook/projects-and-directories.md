# 项目与会话工作目录

runtime workspace 用于保存配置、会话和状态；会话工作目录决定文件工具及新 Shell 操作哪个代码目录。
可以让同一个 runtime 中的不同 Session 使用不同仓库，无需切换 Python 环境或复制配置。

## Web 使用

打开 Web 聊天页的“项目与目录”，或直接访问 `/workspace`。

1. 创建项目，输入名称和运行机器上已有目录的绝对路径。默认目录可以留空，之后绑定一次。
2. 选择“打开项目聊天”。第一条消息创建项目会话，并继承当时的默认目录。
3. 页面下方选择已有会话，查看当前目录、可用状态和仓库规则来源；可以独立切换该会话的目录。
4. “浏览服务端目录”按页列出目录。它显示运行 Kirakira 的机器上的路径。

项目支持改名、归档和取消归档。绑定后的默认目录保持固定；绑定只影响此后新建的会话。
归档项目不再创建新会话，已有会话和历史身份保留。项目不是记忆隔离或文件访问授权边界。

## Agent 工具

让 Agent 先调用 `set_working_directory(path="/absolute/existing/repo")`，并单独占用一次工具批次。
切换成功后，再调用相对路径文件工具或 `bash`。相对目录切换要求当前已经有可用 cwd。

模型同时请求切换和其他工具时，整个批次会被拒绝。Web/控制协议的切换会等待当前轮次结束。
其他 Session 和已经运行的 Shell/PTY 保持原目录。子 Agent 在创建时继承父目录，之后彼此独立。

目录丢失时，恢复目录或显式切换到另一个已有绝对目录。运行时不会自动重建 worktree，
也不会把相对写入悄悄放到 runtime workspace。显式绝对文件路径不依赖失效的 cwd。

## 控制协议

握手完成后，可通过 `agent.control.client.ControlClient.request` 使用：

| 方法 | 参数 |
| --- | --- |
| `project/list` | `{}` |
| `project/create` | `projectId`、`name`，可选 `directory` |
| `project/update` | `projectId`，可选 `name`、`archived`、`expectedRevision` |
| `project/bindDirectory` | `projectId`、`path` |
| `thread/start` | `metadata: {project_id: "p_..."}` |
| `directory/current` | `threadId` |
| `directory/browse` | 绝对 `path`，可选分页 `after` |
| `directory/set` | `threadId`、`path`、稳定 `requestId`、`expectedRevision` |

创建项目的 ID 为 `p_` 加 32 位小写十六进制。创建重试保持原 ID 和原参数。
切换前读取 `directory/current`，把返回的 `revision` 原样放入 `expectedRevision`；
旧会话可能返回 `null`，同样必须显式传入。响应丢失时保持相同 requestId 和参数重试，
不要新建请求来掩盖未知结果。版本冲突时先刷新状态，再明确发起新的操作。

Web 的 `POST /api/workspace` 使用 `{method, params}`，共享这些校验和服务。
这是 Kirakira 的控制 API，不表示兼容上游所有插件 UI 协议。

## 仓库规则

每次新模型生成读取最近 `.git` 根到当前目录的规则，支持 Git worktree 的 `.git` 文件。
同一层优先 `AGENTS.override.md`，再取 `AGENTS.md`，总预算 32 KiB。
修改或删除规则会在下一次生成生效，切换目录也会替换旧规则。
规则不可读或超限时明确提示，不能沿用上次成功读取的正文继续进行依赖它的写入。

当前实现使用本地执行机器的文件系统。完整远程宿主、工作台终端、改动视图和移动端的范围见
[功能清单](../status/akashic-features.md)；持久与失败语义见 [ADR0015](../decisions/0015-projects-and-session-directories.md)。
