# 当前未完成的工作

只保留未完成、正在做或真实阻塞的事项。完成后从本文件删除,历史由 git 与
[decisions/](./decisions/) 负责。规范见 `Reference/docs/writing-rules.md`。

## 1. Phase 模块签名迁移到 frame

**现状**:slot 依赖图已生效(`kirakira_agent/phase.py`),但模块签名仍是 ctx 对象,
模块之间没有通过 `frame.slots` 传中间产物的通道。Reference 的 `PhaseFrame` 因此暂未移植。

**接手点**:相位模块签名改为 `run(frame) -> frame` 时一并引入 `PhaseFrame`。

**验收**:两个模块能通过 slots 传递中间产物,且顺序由 requires 决定而非注册顺序。

## 2. 插件源的真实端到端验证

**现状**:插件声明的 `ProactiveSourceSpec` 已编译成真实源并进入 `SourceRegistry`
(`cli._build_source_registry`),但只有替身工具的契约测试,没有真实 MCP server 的端到端证据。

**接手点**:用一个真实 MCP server 声明一个源,验证 fetch/ack 全链路。

**验收**:真实源产出的事件进入三通道去重、被判断链路消费,ACK 回到源端。

## 3. 其余已知差距(未排期)

| 项 | 状态 |
| --- | --- |
| Drift 的 hazard drive(到期采样触发) | 未实现;journal 与 self-observation 已完成 |
| QQ 两渠道逐字节对齐 Reference(Telegram 已对齐) | 未做 |
| 插件包元数据 manifest、非 git 源解析、独立 doctor 模块 | 未做 |
| control plane / app server、前端 Dashboard、peer-agent、eval | 未做 |
| 主动多目标调度(当前单 `[proactive.target]`) | 未做 |
