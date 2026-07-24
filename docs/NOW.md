# 当前未完成的工作

只保留未完成、正在做或真实阻塞的事项。完成后从本文件删除,历史由 git 与
[decisions/](./decisions/) 负责。规范见 `Reference/docs/writing-rules.md`。

## 1. 记忆:consolidation 驱动权移交

**现状**:归档由 `MemoryRuntime.schedule_consolidation` 驱动;`MarkdownMemoryMaintenance`
已构建但以 `event_bus=None` 接入,不订阅 `TurnCommitted`,只提供四文件读取。

**为什么没直接切**:两边都订阅会重复归档并争抢 `last_consolidated` 游标;且旧路径承载
一条安全行为——待归档消息超阈值且 consolidation 无法推进时拒绝本轮,避免静默丢历史
(`PassiveTurnPipeline._guard_memory_context`),它依赖同步 schedule + wait 语义,
而 maintenance 是异步队列。理由见 [decisions/0002](./decisions/0002-consolidation-driver.md)。

**接手点**:先给异步 maintenance 补等价的 context guard(超阈值且未推进时能让 turn 拒绝),
再把驱动权交给它并删除旧 `_consolidate_session`。

**验收**:①超阈值且归档卡住时 turn 仍被拒绝,不静默丢历史;②同一 session 不会被
归档两次;③`ConsolidationCommitted` 仍按窗口发出,引擎长期提取不回退。

## 2. 依赖注入推广到 context / session

**现状**:只有记忆有 `MemoryServices` 服务包。`PassiveTurnPipeline` 仍直接持有
`ContextBuilder`、`SessionManager` 等具体对象。

**接手点**:照 `Reference/agent/looping/ports.py` 补 `ContextServices` / `SessionServices`,
pipeline 改为消费服务包。

**验收**:替换其中一个子系统的实现时,不需要改 pipeline 调用点与其余子系统的测试。

## 3. Phase 模块签名迁移到 frame

**现状**:slot 依赖图已生效(`kirakira_agent/phase.py`),但模块签名仍是 ctx 对象,
模块之间没有通过 `frame.slots` 传中间产物的通道。Reference 的 `PhaseFrame` 因此暂未移植。

**接手点**:相位模块签名改为 `run(frame) -> frame` 时一并引入 `PhaseFrame`。

**验收**:两个模块能通过 slots 传递中间产物,且顺序由 requires 决定而非注册顺序。

## 4. 主动链路:跨崩溃可靠投递

**现状**:投递与提交已收敛到 `turns.commit_turn_result` 单点,成功才写 Session/起冷却。
仍缺 durable outbox:进程在"渠道成功"与"本地提交"之间崩溃时可能重复发送。

**接手点**:在 `turns.commit_turn_result` 落 outbox 记录,重启时按记录判定是否已投递。

**验收**:模拟在两步之间崩溃并重启,同一条消息不重复发送。

## 5. 插件源的真实端到端验证

**现状**:插件声明的 `ProactiveSourceSpec` 已编译成真实源并进入 `SourceRegistry`
(`cli._build_source_registry`),但只有替身工具的契约测试,没有真实 MCP server 的端到端证据。

**接手点**:用一个真实 MCP server 声明一个源,验证 fetch/ack 全链路。

**验收**:真实源产出的事件进入三通道去重、被判断链路消费,ACK 回到源端。

## 6. 其余已知差距(未排期)

| 项 | 状态 |
| --- | --- |
| Drift 的 journal / self-observation / hazard drive | 未实现 |
| QQ 两渠道逐字节对齐 Reference(Telegram 已对齐) | 未做 |
| 插件包元数据 manifest、非 git 源解析、独立 doctor 模块 | 未做 |
| control plane / app server、前端 Dashboard、peer-agent、eval | 未做 |
| 主动多目标调度(当前单 `[proactive.target]`) | 未做 |
