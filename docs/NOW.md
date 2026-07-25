# 当前未完成的工作

只保留未完成、正在做或真实阻塞的事项。完成后从本文件删除,历史由 git 与
[decisions/](./decisions/) 负责。规范见 `Reference/docs/writing-rules.md`。

## 1. 主动链路 lifecycle 化,Drift 作为其中一个模块

**现状**:主动链路是 `ProactiveLoop._tick()` 一条扁平顺序链(gate → fetch → ingest →
decide → deliver → drift)。Drift 由 `_drift_hook` 在"本轮没推送"时直接调用。

Reference 的形态:主动链路是一个模块流水线(`proactive_v2/lifecycle.py` 318 行 +
`frame.py` 42 + `modules_schedule.py` 51),Drift 是其中一个模块——
`DriftFlowModule` 声明 `slot="drift.flow"`、`requires=("route:selected",)`、
`produces=("proposal:drift",)`,把结果写进 `frame.slots["proposal:drift"]`。

**接手点**:
1. 引入 `ProactiveFrame`(带 `slots` 的帧),即此前刻意推迟的 `PhaseFrame` 的主动链路版本;
2. 用已有的 `phase.topo_sort_modules` 驱动模块顺序(slot DAG 已就绪,可直接复用);
3. 把 tick 的各步拆成声明 slot 的模块,Drift 改为其中一个模块而非 hook。

**验收**:模块间通过 `frame.slots` 传中间产物;执行顺序由 `requires` 决定而非代码行序;
插件可以往主动链路里插自己的模块并声明依赖。

**为什么值得做**:这是 Drift 与主动链路"完整 lifecycle"的实际含义,也是插件能扩展主动
链路的前提。slot DAG 已经建好,缺的是 frame 与模块化本身。

## 2. Phase 模块签名迁移到 frame

**现状**:slot 依赖图已生效(`kirakira_agent/phase.py`),但模块签名仍是 ctx 对象,
模块之间没有通过 `frame.slots` 传中间产物的通道。Reference 的 `PhaseFrame` 因此暂未移植。

**接手点**:相位模块签名改为 `run(frame) -> frame` 时一并引入 `PhaseFrame`。

**验收**:两个模块能通过 slots 传递中间产物,且顺序由 requires 决定而非注册顺序。

## 3. 插件源的真实端到端验证

**现状**:插件声明的 `ProactiveSourceSpec` 已编译成真实源并进入 `SourceRegistry`
(`cli._build_source_registry`),但只有替身工具的契约测试,没有真实 MCP server 的端到端证据。

**接手点**:用一个真实 MCP server 声明一个源,验证 fetch/ack 全链路。

**验收**:真实源产出的事件进入三通道去重、被判断链路消费,ACK 回到源端。

## 4. 其余已知差距(未排期)

| 项 | 状态 |
| --- | --- |
| Drift 的 journal / self-observation / hazard drive | 已完成 |
| QQ 两渠道逐字节对齐 Reference(Telegram 已对齐) | 未做 |
| 插件包元数据 manifest、非 git 源解析、独立 doctor 模块 | 未做 |
| control plane / app server、前端 Dashboard、peer-agent、eval | 未做 |
| 主动多目标调度(当前单 `[proactive.target]`) | 未做 |
