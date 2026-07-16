# Kirakira Agent 与 akashic-agent 差距审计

> 审计基准：reference `6a0616c`（2026-07-16）对比本仓库 `3d0876d`。
>
> **排除范围**：只排除自主主动链路——`proactive_v2`、drift、sensor、energy、judge、presence，
> 以及无人请求时的自主触达。Web、Telegram、QQ、用户创建的定时任务、用户 turn 派生的后台任务
> 都属于被动链路，在审计范围内。
>
> **本文档的规矩**：每一条状态都必须是**核对过代码**得出的，不能靠印象。上一版审计因为凭印象
> 写，把 #123 标成"已跟进"，实际上根本没做（见 §6）。

## 1. 体量对照

| | Reference | Kirakira |
| --- | ---: | ---: |
| Python 总行数 | ~153,000 | ~10,100 |
| 被动链路相关行数（agent+bus+core+session+infra+bootstrap，含少量 proactive） | ~49,000 | ~10,100 |
| 测试文件数 | 135 | 20 |
| 测试项数 | — | 142 |

体量差 15 倍，但其中大部分是**被明确排除的范围**（`proactive_v2` 3.3k、`plugins/` 插件市场
19.2k、`memory2` 5.6k、`eval` 2.3k、前端 Dashboard）以及 reference 更完整的代际/控制面机制。
被动主链路的**行为**覆盖度远高于行数比暗示的程度，但**结构粒度**确实更粗（见 §4）。

## 2. 逐模块映射

### 2.1 被动主链路（核心）

| Reference | Kirakira | 状态 |
| --- | --- | --- |
| `bus/events.py`、`events_lifecycle.py` | `events.py`、`lifecycle.py` | 已覆盖 |
| `bus/queue.py` | `bus.py` | 已覆盖，并测试并发/顺序 |
| `bus/event_bus.py` | `event_bus.py` | 已覆盖（ordered intercept + fanout observer） |
| `agent/looping/*`（1211 行） | `runtime.AgentLoop` | 已覆盖被动消费、串行化、中断 |
| `agent/core/passive_turn.py`（2255 行） | `runtime.PassiveTurnPipeline` + `DefaultReasoner` | 已覆盖 streaming tool loop 与 retry |
| `agent/turns/*` | pipeline 内联 | 行为覆盖，无独立 TurnResult/SideEffect 抽象 |
| `agent/lifecycle/*`（2200 行） | `lifecycle.py` + pipeline | 7 个 phase ctx 已覆盖；无 slot DAG（§4.1） |
| `agent/prompting/assembler.py`、`budget.py` | `context_builder.py` + `runtime._trim_context` | 行为覆盖，结构更轻（§4.2） |
| `agent/retrieval/protocol.py`、`default_pipeline.py` | 直接调用 `memory.build_retrieval_block` | 行为覆盖，无可插拔 pipeline 接缝（§4.3） |
| `agent/model_runtime/context_policy.py` | `context_policy.py` | ✅ 本轮补齐（含 #117 的 160 基准） |
| `agent/model_runtime/*`（其余） | `models/openai_compatible.py` | 我们只有一类后端，无需统一（§5） |
| `session/manager.py`、`store.py` | `session.py` | JSON canonical + SQLite FTS；#124 语义本就一致 |
| `core/memory/*`、`memory2/*`（5.6k） | `memory.py`、`embeddings.py` | 核心行为覆盖，算法简化（§4.4） |

### 2.2 工具与扩展

| Reference | Kirakira | 状态 |
| --- | --- | --- |
| `agent/tools/registry.py`、`base.py` | `tools/registry.py` | 已覆盖 |
| `agent/tool_hooks/*` | `tool_hooks.py` | pre/post/error hook 已覆盖 |
| `agent/tools/{filesystem,shell,web_fetch,web_search,vision,tool_search}.py` | `tools/builtins.py` | 已覆盖 |
| `agent/tools/{memorize,recall_memory,forget_memory,message_lookup,message_push,schedule}.py` | `tools/builtins.py`、`scheduler.py` | 已覆盖 |
| `agent/tools/spawn.py`、`agent/background/*` | `subagent.py` | 已覆盖 inline/background、profile、并发上限 3 |
| `agent/policies/delegation.py` | `subagent.py` 的 `_MAX_BACKGROUND_JOBS = 3` | 行为等价（§6.2） |
| `agent/policies/history_route.py` | 无 | **不需要**：upstream 死代码（§6.2） |
| `agent/mcp/client.py` | `mcp/client.py` | 已覆盖，并已收紧结果结构校验 |
| `agent/mcp/declarations.py`、`host.py`、`generation.py`、`watcher.py` | `mcp/declarations.py`、`host.py`、`publisher.py`、`watcher.py` | ✅ 本轮补齐 |
| `agent/mcp/admin.py`、`agent/tools/workspace_mcp.py` | `mcp/admin.py` | ✅ 本轮补齐（§6.1） |
| `agent/plugins/*`（7950 行） | `plugins.py`、`plugin_manifest.py`、`plugin_decorators.py` | 核心合同覆盖；代际粒度更粗（§4.1） |
| `agent/plugins/snapshot.py` | `snapshot.py` | ✅ 本轮补齐；单一代际而非 per-plugin（§4.1） |
| `agent/skills.py` | `skills.py` | 已覆盖 |
| `agent/tools/agent_restart.py`、`agent/restart.py` | 无 | 有意未做（§5） |

### 2.3 渠道与装配

| Reference | Kirakira | 状态 |
| --- | --- | --- |
| `infra/channels/contract.py`、`base.py` | `channels/contract.py`、`base.py` | 已覆盖 |
| `infra/channels/web_chat_channel.py` | `channels/web.py` | 已覆盖，transport 不同 |
| `infra/channels/telegram_channel.py` | `channels/telegram.py` | 已覆盖 Bot API 行为 |
| `infra/channels/qq_channel.py`、`group_filter.py` | `channels/qq.py` | 已覆盖 OneBot HTTP 行为 |
| `bootstrap/channel_host.py` | `channels/host.py` | 已覆盖启停与失败回滚 |
| `bootstrap/app.py`、`wiring.py`（6.5k） | `cli.build_runtime`、`CoreRuntime` | 已覆盖核心装配 |
| `infra/persistence/json_store.py` | 各模块内联 `_atomic_write` | 行为覆盖，未抽公共模块 |
| `agent/control/*`、`infra/control/*`（1748+ 行） | 无 | 有意未做（§5） |
| `agent/peer_agent/*`（1094 行） | 无 | 有意未做（§5） |
| `frontend/`（React Dashboard） | 无 | 有意未做（§5） |

## 3. 本轮（reference 更新 26 个 commit 后）的处置

| Reference 变更 | 性质 | 处置 |
| --- | --- | --- |
| #120 MCP registry → 声明式热重载 | 子系统重做 | ✅ 已重做 |
| #123 agent 自助管理 workspace MCP | 新能力 | ✅ 已补（本次审计才发现漏了，§6.1） |
| #104 插件描述符 → 程序化能力声明 | 子系统重做 | ✅ 已重做 |
| #105/#119 代际 + RuntimeSnapshot + lease | 新架构 | ✅ 已按比例实现 |
| #117/#116 context policy 派生（640 → 160） | 参数与派生 | ✅ 已跟进 |
| #127 workspace 状态隔离 | 装配 | ✅ 已跟进 |
| #111 fail-loud contracts（236 文件） | 全仓原则 | ⚠️ 择要应用（§4.5） |
| #124 session 裁剪保留历史 | 修复 | ✅ 本就一致（已核对 `session.py` 切片语义） |
| #106 只启用一个 Memory Engine | 修复 | N/A：我们只有一个引擎 |
| #121 supervised restart | 运维 | ❌ 有意未做（§5） |
| #118 TUI IPC → app server | 控制面 | ❌ 有意未做（§5） |
| #126 rolling backup | 运维脚本 | ❌ 有意未做（§5） |

## 4. 保留的实现差异（不是"缺失"，是更轻的实现）

### 4.1 代际粒度

Reference 的 `RuntimeSnapshot` 按 **per-plugin generation** 组织：每个插件有独立代际、独立
skill catalog、独立 MCP catalog，状态机是 `compiled → published_pending → committed/aborted →
retired`，还有发布后验收与回滚、`quiesce`、`fork_lease`、slot 拓扑排序。

Kirakira 的快照是**单一代际**（phase 模块 + MCP catalog + hooks），状态机是
`compiled → published → retired → drained`。语义相同（换代不影响在途 turn、租约排空才回收），
但做不到"只换某一个插件的代际而不动其他插件"。

对当前规模够用；插件多到需要独立换代时再拆。

### 4.2 Context trim

Reference：`prompting/budget.py` 定义具名 `ContextTrimPlan`，按 `drop_sections` 分级丢弃
（`skills_catalog` → `memes` → `long_term_memory` → `retrieved_memory`）。

Kirakira：`runtime._trim_context(messages, level)` 按 level 逐级 microcompact。

行为方向一致（先丢动态上下文再丢历史），但 reference 的分级是**按 prompt section 语义**，
我们的是**按消息粒度**，可解释性更差。

### 4.3 Retrieval 接缝

Reference 有 `MemoryRetrievalPipeline` 协议 + `DefaultMemoryRetrievalPipeline`，被动 turn 依赖
协议而非具体实现，因此可以整体替换检索策略。

Kirakira 的 pipeline 直接调 `memory.build_retrieval_block`。行为一样，但没有替换接缝。
真要做多路召回 + RRF 时（见 `VERSION_EVOLUTION.md §5.5`），第一步就是补这个接缝。

### 4.4 记忆算法

Reference 的 `memory2`/Akasha 有 LLM query rewrite、HyDE、sufficiency checker、profile
extractor、procedure 冲突检测、热度排序、图关系存储。

Kirakira：Markdown + typed records + FTS + 可选 embedding（语义 0.75 + 词法 0.25）。

### 4.5 Fail-loud 范围

Reference #111 是 236 文件、18k 行的全仓收紧，其中大半覆盖我们排除的 proactive/dashboard/
peer-agent。我们提取原则应用于自己的被动链路（memory 写入、session 列举、MCP 结果结构、
plugin manifest），共 4 处 + 5 个测试。**原则一致，覆盖面按范围裁剪。**

### 4.6 其他

- **Phase slot DAG**：Reference phase module 可声明 slot import/export 并拓扑排序；我们是显式顺序链。
- **Transport**：Reference 用 FastAPI/WebSocket 与更重的 SDK；我们用标准库 HTTP + Bot API + OneBot HTTP。
- **持久化抽象**：Reference 有 `infra/persistence/json_store.py` 统一原子写；我们各模块内联。
- **安装向导**：Reference 有 Click setup wizard；我们用 `config.example.toml` + 环境变量。
- **Plugin jobs**：Reference 插件可声明 interval job，与排除的主动自治边界重叠。

## 5. 有意未跟进（记录，不是遗忘）

| 项 | 为什么不做 |
| --- | --- |
| `agent/control/*` + app server（#118） | 控制面形态，不影响被动链路语义；我们只有本进程 REPL |
| supervised restart（#121） | 进程监督属运维形态 |
| rolling backup（#126） | 独立运维脚本 + systemd timer |
| Codex backend 统一（#116） | 我们只有 OpenAI-compatible 一类后端，统一无对象 |
| `agent/peer_agent/*` | A2A 外部 agent 进程管理与轮询，超出被动链路 |
| `frontend/` Dashboard | 前端工程 |
| `plugins/` 插件市场（19.2k） | 具体插件实现，不是 runtime |

## 6. 本次审计发现的错误与真实差距

### 6.1 已修复：agent 无法自助管理 MCP

**上一版审计把 #120/#123 合并标成"✅ 已跟进重做"，但只做了 #120。**

后果是一个**能力回归**：我们跟随 upstream 删掉了 `mcp_add`/`mcp_remove`/`mcp_list`，却没有补上
upstream 同期给出的替代品（`agent/mcp/admin.py` + `agent/tools/workspace_mcp.py`）。于是 agent
完全失去了配置 MCP 的能力——比旧版还弱，只有人手改文件才行。

已补 `mcp/admin.py`，提供 `workspace_mcp_apply` / `workspace_mcp_remove` /
`workspace_mcp_status`，与人手改文件走同一条 reconcile；发布失败回滚声明、每次修改留备份、
`status` 只回显 env 键名不回显值。10 个测试。

**教训**：审计表里合并条目（"#120/#123"）会掩盖漏项。一个 commit 一行。

### 6.2 核对后确认"不是差距"的项

这两项如果只看文件名会误判成缺失，核对代码后确认不需要：

- **`agent/policies/history_route.py`**（意图路由）：`HistoryRoutePolicy` 类**在整个 reference
  里从未被实例化**，只有 `policies/__init__.py` 导出它。是 upstream 的死代码。
- **`agent/policies/delegation.py`**（委派决策）：类型签名看着很重（`heuristic|llm|manual_rule`、
  confidence、reason_code），但实现的 docstring 写得很清楚："限制并发委派数量，其余决策交由
  模型指引"——实际只做并发上限 3。我们 `subagent.py` 的 `_MAX_BACKGROUND_JOBS = 3` 行为等价，
  差的只是结构化决策元数据。

**方法论**：判断差距要看**调用点**，不能看文件名或类型声明。reference 里有 aspirational
的类型脚手架（Literal 列了 llm/manual_rule，实现里根本没有）。

### 6.3 尚未闭合的真实差距（按值得做的程度排序）

1. **Retrieval 接缝**（§4.3）——做多路召回前必须先补，成本低、收益明确。
2. **Context trim 按 section 分级**（§4.2）——可解释性更好，成本低。
3. **per-plugin 代际**（§4.1）——当前规模不需要，插件变多再说。
4. **结构化委派决策元数据**（§6.2）——只有在要做 trace/评测时才有价值。

## 7. 审计证据

- Python：`/home/xiang/.conda/envs/xingshu-vllm/bin/python` 3.12。
- `unittest discover -s tests`：**142 项通过**（本轮新增：context policy 8、snapshot 13、
  workspace 6、fail-loud 5、mcp admin 10；MCP 由 2 项扩展到 16 项）。
- 关键回归 `test_turn_pins_snapshot_tools_across_mid_turn_hot_reload`：turn 中途换代后本轮仍
  调用到旧代际工具并拿到旧代际返回值，全局 current 已是新代际，旧代际在租约释放后 drained。
- 真实 stdio MCP server 端到端：声明 → 连接 → 发布 generation → `/tools` 列出
  `mcp_fake__echo` / `mcp_fake__fail` → 干净关闭。
- 全新 clone 按 README 操作可直接启动，运行时状态自动生成且不被 git 跟踪。
- `git ls-files` 无密钥形状字符串；`Reference/` 由 `.gitignore` 排除。
