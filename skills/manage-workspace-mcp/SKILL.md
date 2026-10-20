---
name: manage-workspace-mcp
description: 为当前原生 Kirakira Runtime 接入、更新或排查 MCP 服务时使用。
---

# MCP 接入

MCP 由原生插件声明和注册。不再读取旧 mcp/servers/*.toml，
也没有 mcp_apply、mcp_status、mcp_remove 工具。

先确认目标服务的所属插件及本代际运行状态。开发扩展时参考
plugins/mcp/contract.py：通过 inject 声明 MCP_SERVERS，使用
McpServerDefinition 提供 argv、cwd、env、required_tools，然后调用
MCP_SERVERS.register(ctx, definition)。消费者通过 open(ctx, name)
取得服务；catalog、failures 用于诊断。

命令必须非交互并遵守 MCP stdio 协议，协议 stdout 不能混入普通日志。
长期服务先通过 managed_processes 或 workloads 合同取得服务句柄，
再声明端点依赖。参考 eval/fixtures/native_extension/plugin.py。

配置结构写入扩展源码；密钥使用私有配置或凭据引用。
发布遵循 plugin-system，核对新代际与工具目录后执行真实工具调用。
失败时保留旧代际和诊断，不跳过依赖健康检查。
本地 Python 命令沿用项目唯一 .venv 的解释器。
