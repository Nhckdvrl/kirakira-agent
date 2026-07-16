# Kirakira Agent 与 akashic-agent 被动链路差异审计

> 本轮审计基于 reference 更新到 `6a0616c`（2026-07-16，比上次审计前进 26 个 commit）后的源码。
> 排除项仍然只有自主主动链路：`proactive_v2`、drift、sensor、energy、judge、presence，以及
> 无人请求时的自主触达。Web、Telegram、QQ、用户创建的定时任务、用户 turn 派生的后台任务仍属
> 被动链路范围。

## 1. 审计结论

Kirakira 覆盖 Reference 被动主链路的关键行为：统一 Channel、MessageBus、会话并发控制、生命周期
phase、streaming tool loop、session、长期记忆、插件、MCP、技能、后台 shell、subagent、调度和
graceful shutdown。

本轮的重要发现是：**Reference 把我们此前照抄的两个子系统整个推翻重做了**，我们跟进了这两处
重做，并补上了它们共同依赖的代际快照机制。详见第 3 节。

## 2. Reference 更新带来的差异（本轮处理）

26 个新 commit 中与被动链路相关的有 11 个。分类与处置：

| Reference 变更 | 性质 | 我们的处置 |
| --- | --- | --- |
| #120/#123 MCP registry → 声明式热重载 | 子系统重做 | ✅ 已跟进重做 |
| #104 插件描述符 → 程序化能力声明 | 子系统重做 | ✅ 已跟进重做 |
| #105/#119 插件代际 + RuntimeSnapshot + lease | 新架构 | ✅ 已按比例实现 |
| #117/#116 context policy 派生（640 → 160） | 参数与派生 | ✅ 已跟进 |
| #127 workspace 状态隔离 | 装配 | ✅ 已跟进 |
| #111 fail-loud runtime contracts（236 文件） | 全仓原则 | ⚠️ 择要应用，见 3.5 |
| #124 session 裁剪保留历史 | 修复 | ✅ 本就一致，见 3.6 |
| #106 只启用一个 Memory Engine | 修复 | N/A：我们只有一个引擎 |
| #121 supervised restart | 运维 | ❌ 未做，见第 5 节 |
| #118 TUI IPC → app server | 控制面 | ❌ 未做，见第 5 节 |
| #126 rolling backup | 运维脚本 | ❌ 未做，见第 5 节 |

## 3. 本轮补齐的差异

### 3.1 MCP：命令式 registry → 声明式热重载

Reference 删除了 `mcp_add`/`mcp_remove`/`mcp_list` 与 `mcp_servers.json`。我们同步删除，改为：

- `workspace/mcp/servers/*.toml`，一文件一 server，文件名必须等于 `name`。
- 严格解析：未知字段、`schema_version != 1`、空 command、非法 env、重名一律拒绝。
- `cwd` 与 `watch_paths` 相对声明文件解析，最终必须落在 `workspace/mcp/` 安全根内，越界拒绝。
- revision 取规范化声明 + watch 内容哈希（长度分帧编码，避免路径与内容拼接碰撞），与 mtime 无关。
- watcher 轮询输入指纹；声明非法时指纹只覆盖原始文件，修好后自动恢复。
- 整批候选语义：任一声明非法或任一 server 连不上，整批作废，旧代际继续服务。
- 删除全部声明会原子发布空代际并排空旧进程。

### 3.2 插件：描述符文件 → 程序化能力声明

`.aka-plugin/plugin.json` 在 Reference 中已不存在。我们同步：

- 插件根目录 `plugin.py` 是唯一入口，也是发现插件的唯一标志。
- 能力由代码声明：`skill_roots()`、`mcp_servers() -> list[McpServerSpec]`、phase 模块、装饰器工具。
- `manifest.toml` 只记录 `plugin_id` + `enabled`；清单损坏时 fail loud，不静默当作全部启用。
- 声明路径在插件自己的加载边界内校验，越界插件失败但不牵连其他插件。
- `plugin_install` 不导入 `plugin.py`：身份取来源目录名，不热执行刚下载的代码。

### 3.3 RuntimeSnapshot 与租约（被动链路核心）

这是本轮最重要的补齐，它让热重载对被动 turn 安全：

- `RuntimeSnapshotStore` 持有 current，publish/commit/rollback 构成换代事务。
- 一个被动 turn 开始时取一份租约，整轮看同一份能力集合。
- 换代只切换 current；在途 turn 继续使用它锁定的那一代，包括那一代的 MCP 连接。
- 旧代际退休后，等最后一个租约释放（`lease_count` 归零）才断开进程（drain）。
- MCP 工具挂在快照上而非共享 ToolRegistry，`SnapshotToolView` 组合基础注册表与本轮快照。
- ContextVar 绑定校验 owner task：子任务必须 `fork()` 自己的租约，不能白嫖父任务的。
- `tool_search` 改为 async，以便在 turn 自己的 task 内读到本轮快照。

### 3.4 Context policy 派生

`memory_window` 与 `output_reserve` 不再是硬编码常数，改为按模型 `context_window` 对 1M 基准
等比例派生（基准 memory_window 已同步 Reference #117 的 160）。显式配置仍然优先。

### 3.5 Fail-loud（择要应用）

Reference #111 是 236 文件、18k 行的全仓收紧，其中大半属于我们不做的 proactive/dashboard/
peer-agent。我们提取其原则并应用于自己的被动链路：

- **memory**：向量检索失败可降级为词法召回（有日志）；向量**写入**失败必须报错，否则会写入
  永远无法被语义召回的记录，并让索引半有半无。
- **session**：列会话时遇到损坏文件报错，与 `get_or_create` 的既有语义一致，不再静默跳过。
- **mcp client**：拒绝非对象 result 与非数组 content，不把畸形结构拼成看似正常的文本。
- **plugin manifest**：清单结构非法直接失败。

判断准则：降级后状态自洽 → 可降级；降级会固化损坏 → 必须报错。

### 3.6 已经一致、无需改动

- **#124 session 裁剪保留历史**：Reference 修复的是"裁剪运行时上下文时误删了 sessions.db 历史"。
  我们的 session 从一开始就是完整历史 + 取窗口切片（`session.py` 的 `messages[-max_messages:]`），
  与 Reference 修复后的语义一致。

## 4. 保留的实现差异

这些不是"主链路缺失"，而是与 Reference 不同的专用实现：

1. **高级记忆算法**：Reference 的 `memory2`/Akasha 还有 LLM query rewrite、HyDE、sufficiency
   checker、profile extractor、图关系存储。Kirakira 提供 Markdown + typed records + FTS +
   optional embeddings。
2. **代际粒度**：Reference 的快照按 plugin generation 组织，每个插件有独立代际、skill catalog
   和 MCP catalog，并有 published_pending/committed/aborted 完整状态机与发布后验收回滚。
   Kirakira 的快照是单一代际（phase 模块 + MCP catalog），够用且语义相同，但没有 per-plugin 代际。
3. **A2A Peer Agent**：Reference 可冷启动外部 A2A 服务并轮询远端任务；Kirakira 只有本地 subagent 和 MCP。
4. **前端 Dashboard**：Reference 带 React Dashboard 与插件面板样式契约。
5. **Transport**：Reference 用 FastAPI/WebSocket 与更重的 SDK；Kirakira 用标准库 HTTP、
   Telegram Bot API 和 OneBot HTTP，功能路径相同。
6. **Phase slot DAG**：Reference phase module 可声明 slot 依赖并拓扑排序；Kirakira 是显式顺序链。
7. **Plugin jobs**：Reference 插件可声明 interval job，与我们排除的主动自治边界重叠。
8. **安装向导**：Reference 有 Click setup wizard；Kirakira 用 `config.example.toml` + 环境变量。

## 5. 已知未跟进项（有意）

- **#121 supervised agent restart**：进程级监督重启，属运维形态而非被动链路语义。
- **#118 app server 控制面**：Reference 用它替换 TUI IPC；Kirakira 只有本进程 REPL。
- **#126 rolling backup**：独立运维脚本 + systemd timer。
- **#116 Codex backend 统一**：Reference 统一了 Codex 与 API-compatible 两类后端；Kirakira
  只有 OpenAI-compatible 一类，统一无对象。

## 6. 明确排除的 Reference 代码

- `proactive_v2/**`、`agent/core/proactive_*`、`drift_turn.py`、`bootstrap/proactive.py`
- proactive source、feedback、presence、energy、judge、sensor、anyaction、quota
- drift skills 自动选择和空闲执行
- 仅服务于 proactive/drift 的 memory optimizer、prompt 和脚本

## 7. 审计证据

- conda Python：`/home/xiang/.conda/envs/xingshu-vllm/bin/python`，Python 3.12。
- `unittest discover -s tests`：132 项通过（本轮新增 context policy 8、snapshot 13、
  workspace 6、fail-loud 5，MCP 由 2 项扩展到 16 项）。
- 关键回归用例 `test_turn_pins_snapshot_tools_across_mid_turn_hot_reload`：turn 中途换代后，
  本轮仍调用到旧代际工具并拿到旧代际返回值，全局 current 已是新代际，旧代际在租约释放后 drained。
- 真实 stdio MCP server 端到端验证：声明 → 连接 → 发布 generation `f130447a65ec` →
  `/tools` 列出 `mcp_fake__echo` / `mcp_fake__fail` → 干净关闭。
- Reference 目录由 `.gitignore` 排除，未提交。
