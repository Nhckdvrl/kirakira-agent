# 与 Reference 的完整对齐清单

> 基准:`Reference/` @ `012e37c8b51df045353972bb551d8e868ab52455`(Akashic Agent)。
> 核对日期:2026-07-27。**本文以代码为准逐模块核对,不以文档或文件同名为证据。**
>
> 本文是**覆盖面**的权威位置:Reference 有什么、kirakira 有没有、差在哪。
> 其他文档的分工:[NOW.md](./NOW.md) 只写"接手点与验收";
> [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md) 写"为什么这样取舍";
> [design/live-verification.md](./design/live-verification.md) 写"哪些真跑过"。

## 0. 怎么读这份清单

状态取值(沿用 DIFFERENCE_AUDIT 的四档,加一档"有意不做"):

| 状态 | 含义 |
| --- | --- |
| **对齐** | 关键输入、状态变化、失败语义、输出都有对应实现与测试 |
| **轻实现** | 用户可见行为成立,但结构、扩展点或恢复合同明显更简单 |
| **替代实现** | 目标相同,因本项目约束选了不同机制,不能声称逐行移植 |
| **未实现** | 运行时能力不存在。有协议、文档或预留接口也不算完成 |
| **有意不做** | 已判断不移植,附理由;不是遗漏 |

一条纪律:**Reference 里没有真实调用点的东西不算差距**,单独标注(本文第 6 节)。

## 1. 规模对照

| | Reference | kirakira |
| --- | --- | --- |
| 产品代码 | ≈91,000 行 | ≈39,000 行(`kirakira_agent/` 36.1k + 顶层镜像 2.9k) |
| 测试代码 | ≈69,100 行 / 185 个文件 | 11,556 行 / 54 个文件 |
| 离线回归 | — | 515 passed, 4 subtests |

差距的大头(akasha 已移植后)在两处:`infra/mobile_realtime`(9.0k)与 `agent/plugins`(8.4k)。
`plugins/akasha`(7.4k)原是第三块,现已移植。

## 2. 逐模块清单:Reference → kirakira

### 2.1 `agent/` — 运行时内核(33,060 行)

| Reference 模块 | 行数 | kirakira 对应 | 状态 | 差在哪 |
| --- | --- | --- | --- | --- |
| `core/passive_turn.py` 等 | 3,380 | `runtime.py:PassiveTurnPipeline` | 轻实现 | 流式工具循环、分级降级重试、context 预检都在;缺流超时独立分支(超时被当成通用异常,不重试直接报"出错") |
| `plugins/` | 8,447 | `plugins.py` + 7 个 `plugin_*.py` | 轻实现 | 声明式规格、代际租约、热重载、安装免重启已对齐;缺包元数据 manifest、非 git 源、版本缓存回滚、为插件 MCP server 准备 venv |
| `tools/` | 5,402 | `tools/builtins.py` + `registry.py` | 轻实现 | 工具清单已齐(见 §3);**缺整个元数据层**:`risk` / `always_on` / `preloadable` / `requires_turn_search` / `search_hint`,以及给每个工具 schema 注入的必填 `description` 进度字段 |
| `lifecycle/` | 2,274 | `lifecycle.py` + `phase.py` | 轻实现 | 只移了**排序器**(拓扑排序逐条对齐);缺 `PhaseFrame` / `Phase` 执行器 / 构造期 slot 闭合校验 / 7 个相位模块文件(1,533 行)。kirakira 相位模块仍是 ctx 直传 |
| `control/` | 1,748 | `control/` (2,305 行) | 对齐 | 协议逐字对齐(12 个方法);errors/events/ids/models 逐字节相同;去 pydantic 改手写校验器(显式偏离) |
| `model_runtime/` | 1,274 | `models/` | 轻实现 | 异步原生 + `tool_choice` 已补;缺 `ResilientLightProvider` 回退、六类错误分类(Auth/RateLimit/Quota/Transport/Retryable/ContextWindow)、provider strategy 分派、`prompt_cache_key` |
| `looping/` | 1,213 | `runtime.py:AgentLoop` | 轻实现 | 保序/并发/中断成立;缺结构化 `InterruptResult` 回执与续跑 `resumed_from_interrupt` 标记(渠道 `/stop` 走的是 bool 版) |
| `mcp/` | 1,208 | `mcp/` | 对齐 | stdio JSON-RPC、声明式发布、失败回滚、代际固定;工具名 `mcp_*`(Reference 是 `workspace_mcp_*`) |
| `peer_agent/` | 1,094 | — | **未实现** | 跨进程 peer agent 管理。Reference 有 14 处真实引用 |
| `background/` | 804 | `subagent.py` | 轻实现 | inline/background + 并发上限 3 成立;**权限模型相反**(kirakira 黑名单过滤父 registry,Reference 白名单构造),新工具默认对 subagent 开放;缺 `exit_reason` 六态与重试协议、`mandatory_exit_tools` |
| `tool_hooks/` | 417 | `tool_hooks.py` | 对齐 | pre/post/error + 超时 |
| `policies/` | 337 | 部分 | 见 §6 | `history_route.py` 是死代码;`delegation.py` 实质是并发上限 3,kirakira 用常量等价 |
| `turns/` | 255 | `turns.py` | 对齐 | `TurnResult` 副作用单点提交;skip 语义已对齐 |
| `prompting/` | 240 | `prompting/` (421 行) | 对齐 | PromptBlock、装配、预算 |
| `retrieval/` | 114 | `retrieval.py` + 引擎 | 对齐 | 检索走 `engine.query`,与 `default_pipeline.py` 同形 |
| `provider.py` | 995 | `models/openai_compatible.py` | 轻实现 | 见 model_runtime |
| `scheduler.py` | 713 | `scheduler.py` | 轻实现 | **能力缩水最严重**:无 cron 表达式、无 soft tier(到点跑一轮完整 agent turn 生成实时内容)、无时区、无命名取消、无延迟自适应。misfire 恢复已补 |
| `config.py` / `config_models.py` | 955 | `config.py` + `_compat/config_models.py` | 轻实现 | 只覆盖 kirakira 实际支持的配置面 |
| `subagent.py` | 397 | `subagent.py` | 轻实现 | 同 background |
| `skills.py` | 317 | `skills.py` | 轻实现 | 单一来源(只扫 workspace);缺 `root_dir`(带附属资源的 skill 无法自定位)、三源合并、XML 式 summary |
| `restart.py` | 276 | `restart.py` | **对齐** | 近逐字移植;实弹换代成功 |
| `supervisor.py` | 297 | `agent/supervisor.py` | **对齐** | 逐字节一致 |
| `context.py` / `memory.py` | 702 | `context_builder.py` / `memory.py` | 轻实现 | — |

### 2.2 `infra/` — 基础设施(13,298 行)

| Reference 模块 | 行数 | kirakira 对应 | 状态 | 差在哪 |
| --- | --- | --- | --- | --- |
| `mobile_realtime/` | 8,992 | — | **未实现** | 移动端实时网关:配对(pairing)、鉴权、密钥保护、附件、远端媒体、inbox、plugin_ui、协议。有真实装配点(`bootstrap/app.py:377`)。**此前所有文档都没记过这块** |
| `channels/` | 3,784 | `infra/channels/` + `channels/` | 分项 | Telegram 五个源文件逐字节一致;Web/QQ/QQBot 是 kirakira 自己的实现 |
| `control/` | 298 | `control/socket.py` + `connection.py` | 对齐 | 仅 import 路径差;缺 `stdio.py`(45 行) |
| `persistence/` | 203 | 分散在各模块 | 轻实现 | `json_store` 的原子写已有等价物;无统一 persistence 语义 |
| `providers/` | 20 | `_compat/provider.py` | 对齐 | — |

### 2.3 `plugins/` — 插件生态(19,862 行)

| Reference 插件 | 行数 | kirakira 对应 | 状态 | 差在哪 |
| --- | --- | --- | --- | --- |
| `akasha/` | 7,353 | `akasha/`(12 文件镜像 + `_compat.py`) | **对齐** | RAR 图激活引擎已移植,doctor 报 `drifted=[]`;跨 session 检索已实弹。未移植 `plugin.py`(依赖 PhaseFrame 与 MobileUI)与 `dashboard.py`(FastAPI),见下 |
| `wake_proactive/` | 3,755 | `proactive/` 部分 | 轻实现 | 缺累计 hazard、embedding 兴趣、turn 原型校准、ack 分类反馈 |
| `drift_flow/` | 2,769 | `drift/` | 轻实现 | SKILL 驱动、journal、hazard 采样、强制收尾已对齐;缺 module factory 与 start/stop rollback |
| `default_memory/` | 2,493 | `coremem/default_engine.py` + `observe.py` | 对齐 | 引擎照抄;RecallInspector 已补(命中项取结构化 records,比 Reference 的正则反解更直接) |
| `default_proactive/` | 2,357 | `proactive/modules.py` | 轻实现 | **缺多层限流**:`AnyActionGate`(日配额+最小间隔+空闲概率门)、`count_deliveries_in_window` 硬闸、context-only 独立限流 |
| `proactive_flow/` | 1,093 | 部分 | 轻实现 | — |
| `wake_*_flow/` | 42 | — | 有意不做 | 只是 21 行的 wake 变体壳 |

**akasha 未移植的两个文件(有意)**:

| 文件 | 行数 | 不移植的理由 |
| --- | --- | --- |
| `plugin.py` | 543 | 依赖 `PhaseFrame`(kirakira 相位模块仍是 ctx 直传)与 `MobileUiContribution`(移动端实时未移植)。它提供的是 `/akashalast` 命令与移动 UI 面板,不是引擎能力 |
| `dashboard.py` | 542 | FastAPI 实现的记忆图面板;kirakira 用零依赖仪表盘,要做应重写而非移植 |

### 2.4 其余顶层(约 14,000 行)

| Reference | 行数 | kirakira | 状态 | 说明 |
| --- | --- | --- | --- | --- |
| `bootstrap/` | 6,652 | `cli.py` + `bootstrap.py` | 轻实现 | setup 向导、init、装配已有;缺 `dashboard_api.py`(1,352,kirakira 用零依赖仪表盘替代)、`chat_api.py`、`app_server.py`(stdio) |
| `memory2/` | 5,618 | `coremem/` | **对齐** | 16 个算法文件逐字节镜像,doctor 报 `drifted=[]` |
| `proactive_v2/` | 3,310 | `proactive/` | 轻实现 | 见 §2.3;另缺 `tick_log`/`tick_step_log` 审计表、strategy trace 落盘、调度权反转 |
| `session/` | 2,815 | `session.py` + `coremem/embedding_store.py` | 轻实现 | JSON canonical + `sessions.db` 派生库(含 Reference 同形的 `messages` 投影与 `messages_fts`,akasha 靠它工作);`embedding_store.py` 逐字节移植;**仍缺跨进程 session admission**、presence、消息稳定 id |
| `core/` | 2,443 | `coremem/engine.py` + `core/net` | 对齐 | 记忆协议逐字节一致 |
| `bus/` | 1,139 | `bus.py` + `event_bus.py` | 轻实现 | MessageBus 对齐;EventBus 缺 snapshot 租约、异常上报、`on_any`、observe 串行队列 |
| `scripts/` | 1,162 | — | 有意不做 | 构建/迁移脚本(含 node 构建链) |
| `prompts/` | 546 | `prompting/` | 轻实现 | Reference 的 proactive/background prompt 更长 |
| `sdk/python` | 526 | — | **未实现** | 给外部程序用的 Python 客户端。kirakira 有 `control/client.py`,但未包装成可分发 SDK |
| `plugin_packages/` | 212 | — | 未实现 | 可分发插件包格式 |
| `schema/*.json` | — | — | 未实现 | app-server 与 mobile-realtime 的协议 schema |
| `types/*.d.ts` | — | — | 有意不做 | 前端 TS 类型 |
| `frontend/` | — | `channels/web_ui.py` | 替代实现 | React+Vite+Tailwind → 零依赖 stdlib 六面板仪表盘 |
| `eval/` | — | — | **未实现** | 评测体系 |
| `docker/` | — | — | 有意不做 | 部署编排 |

## 3. 工具面逐项对照

模型可见的能力面,这是最该逐条核的一层。

| Reference 工具 | kirakira | 状态 |
| --- | --- | --- |
| `shell` | `bash` | 对齐(改名);Ref 有 `cwd`/必填 description/网络策略/restricted_dir |
| `task_output` / `task_stop` | 同名 | 对齐 |
| `filesystem` | `read_file`/`write_file`/`edit_file`/`list_dir` | 对齐;Ref 有 80K 硬上限与二进制/图像分支 |
| `memorize`/`recall_memory`/`forget_memory` | 同名 | **对齐**;schema 由 `engine.tool_profile()` 声明,含 `§cited:` 引用协议 |
| `message_lookup` | `search_messages`/`fetch_messages` | 对齐 |
| `message_push` | 同名 | 对齐 |
| `request_user_confirmation` | 同名 | 对齐;`minLength`/`maxLength` 从 schema 挪到运行时(模型看不到 500 字上限) |
| `schedule` | `schedule`/`list_schedules`/`cancel_schedule` | **轻实现**,schema 严重收窄(见 §2.1 scheduler) |
| `spawn` | `spawn`/`spawn_manage` | 轻实现;描述从 40 行缩到单行 |
| `tool_search` | 同名 | 轻实现;Ref 走索引后端 + 本轮授权域,kirakira 内联评分 + session 级 LRU |
| `vision` | 同名 | 轻实现;Ref 条件注册(多模态主模型下不注册),kirakira 无条件 |
| `web_fetch` / `web_search` | 同名 | 对齐;`web_search` schema 更窄 |
| `workspace_mcp_*` | `mcp_apply`/`mcp_remove`/`mcp_status` | 对齐(改名) |
| `agent_restart` | 同名 | **对齐**;deferred 近似 Ref 的 `requires_turn_search` |
| engine 自定义工具槽(`profile.tools`) | 已支持 | **对齐**;akasha 的 `reinforce_memory` 经此注册,handler 是通用回执(对照 Reference `_MemorySignalTool`) |
| — | `compact` | kirakira 独有(接真实归档) |
| — | `plugin_list/doctor/install/enable/disable/uninstall` | kirakira 独有(Reference 只在 CLI) |

**全工具面的共性差**:Reference 给每个工具 schema 注入一个必填的 `description` 进度字段
(要求模型用 5–12 字说明本次调用意图),kirakira 没有。这是模型可见行为的系统性差异。

## 4. kirakira 独有(Reference 没有的)

不是所有差异都是缺口。以下是 kirakira 多出来的:

| 能力 | 说明 |
| --- | --- |
| `memory_admin.py` | M0/M1 迁移体系:backup/migrate/verify/rollback/clear + `structured-owner.json` + **Reference 源码漂移审计**(16 文件逐字节比对)。Reference 从一开始就是 memory2,没有迁移需求 |
| 零依赖仪表盘 | 六面板 + 聊天页,不引构建链 |
| `plugin_*` 工具面 | 插件全生命周期对模型可见 |
| 主动投递语义更强 | 发送前落投递意图 → **至多一次 + 窗口内不重复**;Reference 是发送后标记(崩溃会重发) |
| `compact` 工具 | 模型可主动触发归档 |
| 观测记录有大小上限 | Reference 的 `recall_inspector.jsonl` 是无界 append |
| 装配期编译主动流水线 | 排序失败 fail loud;Reference 也是构造期编译,但 kirakira 删掉了自己曾有的"每 tick 重排+静默降级" |

## 5. 完整缺口清单(按影响排序)

### P0 — 改变模型可见行为

1. **ToolRegistry 元数据层**:`risk` / `always_on` / `preloadable` / `requires_turn_search` /
   `search_hint` / 强制 `description` 进度字段全缺。影响全工具面。
2. **scheduler 无 cron / 无 soft tier**:"每天早上 9 点告诉我天气"这类需求**无法表达**。
3. **工具描述密度**:Reference 是决策树式长描述(spawn 40 行、shell 15 行),kirakira 多为单行英文。
5. **检索注入阈值**:实测召回到但未注入(见 [live-verification](./design/live-verification.md) §11)。

### P1 — 可靠性

6. **subagent 权限模型是黑名单**:新注册工具默认对 subagent 开放。
7. **无 light provider 回退与错误分类**:轻模型挂了整条链路挂;限流/额度退化成通用异常。
8. **降级重试不落库裁剪**:`trim_history_async` 缺失,下一轮重现超长历史,降级不收敛。
9. **EventBus 事件路径不持 snapshot 租约**,且 observer 异常静默。
10. **无跨进程 session admission**:只有进程内 `asyncio.Lock`。
11. **流超时无独立分支**:超时被当成通用异常,不重试直接报错。
12. **主动链路多层限流缺失**:只有单层 content 冷却,无日配额/最小间隔/空闲概率门。
13. **全源失败与"今天没新事件"不可区分**:`_safe_fetch` 吞掉一切。

### P2 — 结构与扩展性

14. **PhaseFrame 与 7 个相位模块**(1,533 行):模块间无 slots 通道,`produces` 声明是死约定。
15. **主动链路服务化**:模块仍持有整个 loop。
17. **主动调度权反转**:模块无法影响下次唤醒间隔。
18. **主动审计厚度**:无 `tick_log`/`tick_step_log`/strategy trace。
19. **ack 无分类反馈**:不区分 interesting/not_interesting/discarded。
20. **skill 缺 `root_dir`**;`create-drift-skill` 未移植。
21. **消息无稳定 id**:位置寻址,阻断消息删除与更强的 evidence 语义。
22. **插件安装缺版本缓存回滚、非 git 源、包 manifest、MCP venv 准备**。
23. **中断无结构化回执与续跑标记**;**spawn 无 exit_reason 六态**。

### P3 — 整块未实现的子系统

24. **`mobile_realtime`(8,992 行)**:移动端实时网关全套。
26. **`peer_agent`(1,094 行)**:跨进程 peer 管理。
27. **`sdk/python`(526 行)**:外部程序客户端 SDK。
28. **`eval`**:评测体系。
29. **stdio app_server**、**Dashboard HTTP API**、**chat_api**。
30. **plugin_packages**、**协议 schema**、**QQ 两渠道逐字节对齐**、**主动多目标调度**。

## 6. 已确认不是差距的项

| 项 | 理由 |
| --- | --- |
| `HistoryRoutePolicy` | Reference 里只有定义 + 导出,零实例化调用点(复核 2026-07-27) |
| `DelegationPolicy` | 实质是并发委派上限 3;kirakira 用常量等价。缺的只是结构化 reason/confidence 元数据 |
| 主动多目标调度 | Reference 也是单目标(`sensor.py:33`);`presence.py` 的多 session API 仅被测试引用 |
| proactive `start()` 回滚 | Reference 生产代码零个 starter 实现,只有测试里有一个 |
| `build_proactive_loop` / `_tick_admitted` | Reference 内零调用点 |
| `MemoryEngine.ingest()` | 两边都无调用点(摄入走事件) |
| `reinforce_items_batch` | 两边都无生产调用点 |

## 7. 一句话定位

> kirakira 覆盖了 Reference 的**单机个人 Agent 运行时**:被动链路、记忆引擎、主动推送、
> Drift、插件热重载、控制面、换代重启、仪表盘,均有真实调用点与测试,关键链路已实弹。
> 未覆盖的是**产品化与规模化的外围**:移动端实时、第二记忆引擎、peer 进程、SDK、评测,
> 以及工具元数据层、scheduler 表达力、多层限流这些**厚度差**。
>
> 不能说"已复刻 Akashic";可以说"以约 43% 的代码量覆盖了它的核心运行时语义,
> 外围产品能力与工程厚度仍有明确差距,且差距已逐条列在本文"。
