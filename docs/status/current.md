# 当前状态

更新日期：2026-08-15。

## 已完成

- Runtime 已按 `agent/`、`bootstrap/`、`bus/`、`core/`、`infra/`、`session/`、
  `memory2/`、`plugins/`、`proactive_v2/` 等 owner 收敛；`kirakira_agent/` 只保留公开入口。
- 项目不依赖本地 `Reference/`。移走该目录后，全量测试、构建和 CLI 都能完成。
- Session 使用 SQLite 作为权威存储；正常保存为 append-only。上下文压力只改变当前请求投影，
  不删除历史。
- Context compaction 已对齐最新机制：74% soft gate、输出预留 hard gate、完整 interaction unit、近 20k token 原文、滚动结构化摘要、持久 session ledger 和临时 active-turn 压缩。
- Shell 支持前台/后台、PTY、`write_stdin`、增量输出、取消和进程组清理。
- 子 Agent 有独立 Session 和执行 owner，并共享全局容量限制。
- Scheduler 支持 one-shot、duration、interval、5/6 段 cron、IANA timezone、instant/soft tier。
- Default Memory 与 Akasha v1 都能实际使用；Akasha v1 已通过在线摄入和召回验证。
- Proactive 已有模块 DAG、snapshot lease、ACK/feedback、delivery dedup 和 tick/step trace。
- Workspace 使用单实例锁和 Yoyo migration ledger。
- 已有语义合同、change-impact gate、全量测试和真实 DeepSeek 在线 smoke test。

## 明确延期

1. **Akasha v2**：当前继续使用可工作的 Akasha v1。以后在现有 memory plugin 边界内替换。
2. **前端与移动后端对齐**：保留现有 TUI、Web 和 dashboard；暂不复刻 Android pairing、
   mobile realtime 与 WebUI OTA。
3. **完整 benchmark 与运维体系**：当前不复刻 Reference 的 Docker/Harbor 私有 campaign、
   systemd 和全部 Linux guardian 合同。

## 可以继续加厚，但不阻塞当前使用

- Proactive 的日配额、累计 hazard、兴趣 embedding 和更细的数据源失败策略。
- 更多 provider profile、reasoning effort 与设置 UI。
- 插件非 Git 来源、版本缓存和独立 MCP venv。
- Agent 轨迹的专用前端页面。后端已有 `tool_chain`、`context_trace`、`tick_log` 和
  `tick_step_log`。
- 渠道 turn 与 restart commit 的完整联动；控制面重启链已经可用。

完整差异见 [Reference 对齐状态](./reference-alignment.md)。
