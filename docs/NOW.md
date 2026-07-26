# 当前未完成的工作

只保留未完成、正在做或真实阻塞的事项。完成后从本文件删除,历史由 git 与
[decisions/](./decisions/) 负责。规范见 `Reference/docs/writing-rules.md`。

> 2026-07-26 起的取向(用户指示):**功能真实还原优先,结构工程后补**。
> 因此本文件分两节:第 1 节是功能/可靠性缺口,第 2 节是明确推迟的结构工程。

## 1. 功能与可靠性缺口

### 1.1 换代冻结窗口的并发拒绝观察

**现状**:agent_restart 真实换代已实弹通过(2026-07-26,见
[design/live-verification.md](./design/live-verification.md) 第 9 节:模型自己
tool_search → arm → 送达 → 75 → 第二代拉起)。冻结窗口内"另一连接拿到 retryable
拒绝"只有单测,没在真实换代中并发观察过。

**验收**:换代进行中并发第二个控制面连接,收到 `-32011` 且 `retryable=true`。

### 1.2 渠道 turn 发起的重启

**现状**:`agent_restart` 只在控制面 turn 内有 `current_turn_id`;渠道 turn 调用会被
`arm` 明确拒绝("缺少完整 turn 上下文"),**快速失败而不是挂到 watchdog 超时**。
Reference 用 `OutboundMessage.control_turn_id` + 渠道投递观察者覆盖这条
(`bootstrap/app.py:328-343`)。

**接手点**:被动 turn 也分配 turn id(Reference `looping/core.py:675`),渠道投递成功后
`mark_delivered`。

**验收**:从 Telegram 让 agent 自重启,消息送达后进程换代。

### 1.3 插件源的真实端到端验证

**现状**:插件声明的 `ProactiveSourceSpec` 已编译成真实源并进入 `SourceRegistry`;
tick 现在把租到的 snapshot 钉在共享 gateway 上,源在本轮用同一代 MCP 工具视图。
仍缺真实 MCP server 的 fetch/ack 证据(见
[design/live-verification.md](./design/live-verification.md) 第 8 节)。

**验收**:真实源产出的事件进入三通道去重、被判断链路消费,ACK 回到源端。

### 1.4 主动链路的限流与审计厚度(Reference 有、kirakira 无)

按对 Reference 的代码核实(2026-07-26),以下机制在 Reference 有真实调用点:

| 机制 | Reference 位置 | 影响 |
| --- | --- | --- |
| `count_deliveries_in_window` 硬闸 | `proactive_v2/state.py:266` + `default_proactive/gate.py:51` | 窗口内已投递则整轮拦截 |
| `AnyActionGate`(日配额+最小间隔+空闲概率门) | `default_proactive/anyaction.py:190` | kirakira 只有单层 content 冷却 |
| context-only 独立限流 | `default_proactive/gate.py:74` | 无对应 |
| `tick_log` / `tick_step_log` 审计表 | `proactive_v2/state.py:378` | kirakira 只有扁平 `decisions` 表 |
| ack 分类反馈(interesting/not_interesting) | `mcp_sources.py:161` + `resolve.py:90` | kirakira ack 无 feedback |
| 全源失败抛错(≠今天没新事件) | `mcp_sources.py:85` | kirakira `_safe_fetch` 吞掉一切 |
| 调度权反转(`run:next_wakeup` terminal slot) | `proactive_v2/loop.py:428` | 模块无法影响下次唤醒间隔 |
| 累计 hazard / embedding 兴趣 / turn 原型 | `wake_proactive/hazard.py` + `runtime.py:567-654` | 主动侧排序仍是 severity/newness |

### 1.5 其余已核实的行为差(2026-07-26 扫盘)

| 项 | 说明 |
| --- | --- |
| scheduler 无 cron 表达式/soft tier("每天 9 点的实时内容"无法表达) | Reference `scheduler.py:110,652` |
| ToolRegistry 无 risk/search_hint/强制进度 description 元数据层 | Reference `tools/registry.py:251-330` |
| 轻模型无 `ResilientLightProvider` 回退与六类错误分类 | Reference `model_runtime/fallback.py` |
| 降级重试成功后不落库裁剪(`trim_history_async`),下一轮重现超长 | Reference `session/manager.py:520` |
| 渠道中断无结构化回执与续跑标记 | Reference `looping/core.py:513-612` |
| spawn 回传缺 exit_reason 六态与重试协议 | Reference `agent/subagent.py:114` |
| SkillRecord 缺 root_dir(带附属资源的 skill 无法自定位);`create-drift-skill` 未移植 | Reference `agent/skills.py` |
| EventBus 事件路径不持 snapshot 租约,observer 异常静默 | Reference `bus/event_bus.py:190,460` |
| 记忆引擎插件路由未接(`config.memory.engine` 从未被读) | Reference `bootstrap/memory.py:36` |
| Reference 工具描述是决策树式长文,kirakira 多为单行(隐性行为差) | — |

### 1.6 换 provider 后的契约面回归

`post_response_worker` 两处裸 JSON 数组解析已由 `coremem/compat_worker.py` 容错;
`ProactiveJudge` 严格 JSON 与 `finish_drift` 解析在真实模型下跑过;tool_choice 的
required/具名强制已在 deepseek-v4-flash 下实弹通过(见
[design/live-verification.md](./design/live-verification.md) 第 9 节)。换 provider 或
开 thinking 时,这三处解析 + tool_choice 顺从度都要重验(Reference `DeepSeekStrategy`
提示具名强制可能需要同步关 thinking)。

## 2. 明确推迟的结构工程(用户指示"完整的工程以后再补")

| 项 | 说明 | 接手点 |
| --- | --- | --- |
| 主动链路 ProactiveServices 服务化 | 模块仍持有整个 loop;Reference 是 loop→scope→runtime→modules 四层(`proactive_v2/runtime_scope.py` + `default_proactive/runtime.py:109`) | 照 `MemoryServices` 拆服务包;验收:单测模块不需构造整个 loop |
| Phase 模块签名迁到 frame | Reference `PhaseFrame(Generic[I,O])` + `Phase` 执行器 + 构造期 slot 闭合校验(`agent/lifecycle/phase.py:61,301-355`),7 个相位都有真实调用点;kirakira 只有排序器,模块仍是 ctx 直传 | 迁移时注意:`Phase._validate` 会让"requires 了没人 produce 的资源 slot"在启动期直接炸,先跑 `plugins.py:phase_report()` 摸清现有声明 |
| `produces` 声明承重 | kirakira `phase.py` 从不读 `produces`,数据 slot 依赖是死约定;Reference 用 `_expand_dependencies` 把数据依赖翻译成模块依赖(`proactive_v2/lifecycle.py:247-284`) | 与 frame 迁移一起做 |
| proactive 模块 factory / start-stop | Reference 有 module factory 链;但 `start()` 生产代码零实现(只有测试),`stop()` 只有一个实现 | 插件真要贡献主动模块时再做;`add_modules` 目前无生产调用点 |
| QQ 两渠道逐字节对齐(Telegram 已对齐) | — | — |
| 插件包元数据 manifest、非 git 源、版本缓存回滚、MCP venv 准备 | Reference `plugins/install.py:238-551` | — |
| peer-agent、eval、主动多目标调度 | 多目标调度 Reference 也没有(presence 多 session API 仅测试引用) | — |
| Dashboard 的写操作面(记忆批量删除 UI、消息级管理) | 当前仪表盘是只读投影 + 记忆单条失效/会话删除;Reference 有 messages 批量删除与 memory optimizer | 真有运维需要时再加,避免仪表盘变成第二个控制面 |
| RecallInspector 检索回放面板 | Reference `plugins/default_memory/dashboard.py` 读 `observe/recall_inspector.jsonl` 逐轮回放检索命中;kirakira 只在 turn metadata 里留了三字段 trace | 先补检索 trace 落盘,再做面板 |
