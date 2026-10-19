# 配置 Workspace MCP

非插件 MCP server 写在 `<workspace>/mcp/servers/*.toml`。一个文件对应一个 server，文件名必须与
`name` 相同。运行时按内容 revision 热重载，不再使用旧的 `mcp_servers.json`。

```text
<workspace>/mcp/
├── servers/fitbit.toml
└── fitbit-mcp/
    └── run_mcp.py
```

```toml
schema_version = 1
name = "fitbit"
command = ["python", "run_mcp.py"]
cwd = "../fitbit-mcp"
watch_paths = ["../fitbit-mcp/run_mcp.py"]

[env]
LOG_LEVEL = "INFO"
```

## 路径和校验

`cwd` 与 `watch_paths` 相对声明文件解析，最终必须位于 `<workspace>/mcp/` 内。声明只允许
`schema_version`、`name`、`command`、`cwd`、`env`、`watch_paths`；未知字段和错误版本会直接拒绝。

revision 由文件内容和 watch path 内容计算，不依赖 mtime。只 `touch` 文件不会触发换代。

## Agent 管理工具

| 工具 | 作用 |
| --- | --- |
| `workspace_mcp_apply` | 新建或更新声明，并立即校验、发布 |
| `workspace_mcp_remove` | 删除声明并发布新代际 |
| `workspace_mcp_status` | 查看声明、代际和最近错误 |

这些 deferred tools 需要先用 `tool_search` 解锁。声明通过预校验后先持久化选择，再接入运行时。
连接失败保留当前选择及错误；替换前内容保存在 `.toml.previous.bak`，不自动恢复旧实例。
`status` 只显示 env 键名，不回显密钥值。下一次模型生成读取当前可用工具。

## 局部更新语义

- 解析或路径校验失败时不应用非法声明。
- 每个有效 server 选择独立应用；未改变的连接保持身份。
- 改变的 server 先撤销旧入口，等待已接纳的调用，再释放连接并接入当前选择。
- 连接失败保持不可用及诊断，不自动把旧实现恢复成运行事实。
- 删除声明会撤销相应工具和连接，其他 server 不重启。

原理见[所有权与局部更新](../architecture/ownership-and-reload.md)。

## 排查

REPL 的 `/tools` 显示当前代际的 MCP 工具，名称形如 `mcp_<server>__<tool>`。看不到工具时先检查
`workspace_mcp_status` 的声明和 `lastError`。MCP 工具默认 deferred，不会一直占用 prompt。
