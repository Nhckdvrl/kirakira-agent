# 主动链路 lifecycle 化:从扁平 tick 到模块流水线

- 状态:accepted;默认模块集与 Drift 已迁完,主动链路的 DI 服务化仍是 G
- 核对基线:`Reference/` @ `012e37c8b51df045353972bb551d8e868ab52455`
- 核对日期:2026-07-25
- 目标读者:维护者、要往主动链路插模块的插件作者
- 关联:[decisions/0001](../decisions/0001-plugin-slot-ordering-opt-in.md)(slot 排序)、
  [NOW.md](../NOW.md)

标注约定沿用 Reference:**F** 已从代码确认;**I** 结构推断;**G** 当前未给出完整答案。

## 1. 问题

`ProactiveLoop._tick()` 原本是一条扁平顺序链:gate → fetch → ingest → decide → deliver →
(空则) drift。步骤之间靠**代码行序**耦合,提前结束靠 `return`。

由此产生两个具体问题:

1. **插件无法在中间插一步。** 想在 ingest 之后、判断之前加一个过滤,只能改 runtime 源码。
   而插件体系其余部分(工具、相位模块、主动源、作业、服务)都已经可以声明式扩展,主动链路
   是唯一的例外。**F**
2. **顺序不可表达。** 两段逻辑谁先谁后,只能读代码行数看出来,没有地方声明"我依赖谁"。

Reference 的形态是模块流水线(`proactive_v2/lifecycle.py` 318 行 + `frame.py` 42 +
`modules_schedule.py` 51),Drift 只是其中一个模块:`DriftFlowModule` 声明
`slot="drift.flow"`、`requires=("route:selected",)`、`produces=("proposal:drift",)`,
把结果写进 `frame.slots["proposal:drift"]`。**F**

## 2. 当前调用链与状态 owner

```text
ProactiveLoop.run()  ── 电量模型决定 tick 间隔(未改动)
        │
        ▼
    _tick()
        │  new_proactive_frame(session_key)
        ▼
  topo_sort_modules(self._modules)      ← 顺序由 requires 决定,不是注册行序
        │
        ├─ proactive.gate           → gate:passed        （目标就绪 + 被动空闲 + ACK 重试）
        ├─ proactive.fetch          → fetch:channels     （并发拉源）
        ├─ proactive.ingest         → ingest:new_content / ingest:context_text
        ├─ proactive.judge_context  → context:judge      （记忆/近期对话/近期主动/规则面板）
        ├─ proactive.alert          → proposal:alert
        ├─ proactive.content        → proposal:content
        └─ proactive.drift          → proposal:drift
```

状态 owner 未变:`proactive.db` 仍由 `ProactiveStateStore` 独占;模块只是调用它,不各自持有连接。**F**

## 3. 关键设计:`return` 怎么翻译成流水线语义

扁平链用 `return` 提前结束(gate 未过、alert 已推、投递失败)。流水线里没有 return 可用——
每个模块都会被依次调用。

做法是在 frame 上标记终止:

```text
frame.finish("alert_pushed")     ← 模块声明"本轮到此为止",并记下原因
        │
        ▼
后续模块 run() 开头检查 frame.done → 直接返回,不执行 execute()
```

两个细节:

- **第一个原因胜出。** `finish()` 不覆盖已有 terminal,因此"本轮为什么结束"是确定的,
  不会被后面的模块改写。**F**
- **检查放在基类 `run()` 里**,不是每个模块自己判。否则"上一步已结束"会变成七处各写一遍的
  隐式约定,漏一处就是一个只在特定分支出现的 bug。**F**

## 4. 失败与降级

| 情况 | 行为 |
| --- | --- |
| 模块声明成环 / slot 重复 | 记 error 并**保持注册顺序**继续跑,不把主动链路打挂 |
| 渠道投递失败 | 模块 `finish("*_delivery_failed")`,未读保留,不消费事件(与重构前一致) |
| 单个 source 拉取失败 | 由 `SourceRegistry` 吸收,本轮其余源照常(未改动) |

装配错误不阻断运行,这与插件体系既有取向一致:一个坏声明只应影响它自己。**F**

## 5. 迁移与验收

行为等价是硬要求:重构后主动链路既有 20 个测试**全部未修改即通过**。**F**

新增 9 个契约测试覆盖:frame 的 slot/terminal 语义、默认流水线的依赖顺序、
**乱序注册后顺序不变**(证明顺序真由 requires 决定)、插件模块按 requires 落位、
terminal 短路跳过后续模块、Drift 模块的记录与声明形状。

## 6. 仍未解决(G)

- **模块仍持有整个 `loop`。** 理想形态是每个模块只依赖自己需要的服务包(如记忆链路的
  `MemoryServices`)。现在先把顺序与依赖显式化,服务拆分留到主动链路也做 DI 时一起做。
- **`frame.output` 未使用。** Reference 的 `ProactiveTickResult`(base_score /
  next_interval_seconds)让模块能反过来影响调度;我们的电量调度仍在 `_tick()` 之外独立计算。
  按"不摆没人用的结构"的既有取向,暂不引入该字段。
- **主动 tick 没有代际租约。** 被动 turn 已有 per-plugin 代际租约,一次完整 proactive tick
  尚未绑定,热重载可能在 tick 中途换掉模块。
