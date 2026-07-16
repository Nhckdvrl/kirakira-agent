# Workspace MCP 声明

非插件的 MCP server 用 `<workspace>/mcp/servers/*.toml` 声明；一个文件一个 server，文件名必须
与 `name` 一致。运行时按**内容 revision** 热重载，不再读取 `mcp_servers.json`，也不提供
`mcp_add`、`mcp_remove`、`mcp_list` —— 这些工具已经删除。

## 目录布局

```text
<workspace>/mcp/
├── servers/
│   └── fitbit.toml        ← 声明
└── fitbit-mcp/            ← server 自己的代码
    ├── run_mcp.py
    └── src/
```

`servers/fitbit.toml`：

```toml
schema_version = 1
name = "fitbit"
command = ["python", "run_mcp.py"]
cwd = "../fitbit-mcp"
watch_paths = ["../fitbit-mcp/run_mcp.py", "../fitbit-mcp/src"]

[env]
LOG_LEVEL = "INFO"
```

## 路径规则

`cwd` 和 `watch_paths` **先相对声明文件所在目录解析**，最终路径必须落在 `<workspace>/mcp/`
这个安全根内。所以 `../fitbit-mcp` 合法，越出 `mcp/` 会被拒绝。

`watch_paths` 支持文件、目录，或尚未创建的路径；新增、修改和删除都会改变 revision。

## 核心约束

1. **文件名即身份**：`fitbit.toml` 里必须写 `name = "fitbit"`，不一致直接拒绝。
2. **字段是封闭集合**：只允许 `schema_version`、`name`、`command`、`cwd`、`env`、`watch_paths`。
   出现未知字段直接拒绝，不静默忽略（拼错的字段名必须当场暴露，而不是"配了没生效"）。
3. **`schema_version` 必须是 1**：为将来的破坏性变更留出边界。
4. **revision 取内容，不取 mtime**：`touch` 文件不会触发重连，真改了内容才会。
5. **整批语义**：任一声明非法、或任一 server 连不上，**整批候选作废，旧代际继续服务**。
   不存在"三个连上了、第四个没连上"的半完成状态。
6. **失败可自愈**：改坏了不用重启。watcher 每轮重新计算输入指纹，文件修好后自动重试。
7. **删空 = 发布空代际**：删掉所有 `.toml` 或整个 `servers/` 目录，会原子发布一个空代际，
   并排空旧 MCP 进程。这是合法操作，不是错误。

## 失败会怎样

| 情况 | 结果 |
| --- | --- |
| 某个 `.toml` 语法错 / 字段非法 | 整批拒绝，旧代际继续服务，`status().lastError` 有原因 |
| 某个 server 起不来 | 同上；本批已连上的其他 server 会被断开，不留残留进程 |
| 改回正确内容 | 下一轮轮询自动恢复，无需重启 |
| 内容改回与当前代际相同 | 不换代（revision 没变），这是预期行为 |

**改配置文件不会搞挂正在跑的 agent。** 最坏情况是新配置没生效，旧的继续工作。

## 与被动 turn 的关系

MCP 工具**不进共享 ToolRegistry**，只挂在运行时快照上。换代时正在跑的 turn 不受影响，它会
继续使用自己开始时锁定的那一代（包括那一代的连接）；旧进程等最后一个租约释放后才断开。

详见 [snapshot-and-lease.md](./snapshot-and-lease.md)。

## 排查

REPL 里 `/tools` 会列出**当前代际**的 MCP 工具（`mcp_<server>__<tool>`）。看不到就说明这一代
没有它——检查声明是否被拒绝，而不是怀疑工具丢了。

MCP 工具是 deferred 的：模型必须先 `tool_search` 解锁才能调用，这是刻意的，避免远端 schema
一直占用上下文。
