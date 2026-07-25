# 当前未完成的工作

只保留未完成、正在做或真实阻塞的事项。完成后从本文件删除,历史由 git 与
[decisions/](./decisions/) 负责。规范见 `Reference/docs/writing-rules.md`。

## 1. 主动链路模块的服务化

**现状**:主动链路已是模块流水线(见
[design/proactive-lifecycle.md](./design/proactive-lifecycle.md)),但每个模块仍持有整个
`ProactiveLoop`,通过 `loop._state` / `loop._cfg` 取用一切。

**接手点**:照记忆链路的 `MemoryServices`,给主动链路拆出服务包,模块只依赖自己需要的部分。

**验收**:替换某个模块的实现或单测某个模块时,不需要构造整个 ProactiveLoop。

## 2. 主动 tick 的代际租约

**现状**:被动 turn 已在整个 turn 期间持有 per-plugin 代际租约;一次完整 proactive tick
尚未绑定,热重载可能在 tick 中途换掉模块。

**接手点**:tick 开始时 `PluginGenerationRegistry.lease_active()`,结束释放。

**验收**:tick 进行中发生插件换代,本轮仍用开始时的模块集合跑完。

## 3. Phase 模块签名迁移到 frame

**现状**:slot 依赖图已生效(`kirakira_agent/phase.py`),但模块签名仍是 ctx 对象,
模块之间没有通过 `frame.slots` 传中间产物的通道。Reference 的 `PhaseFrame` 因此暂未移植——主动链路已有对应的 `ProactiveFrame` 可作参照。

**接手点**:相位模块签名改为 `run(frame) -> frame` 时一并引入 `PhaseFrame`。

**验收**:两个模块能通过 slots 传递中间产物,且顺序由 requires 决定而非注册顺序。

## 4. 插件源的真实端到端验证

**现状**:插件声明的 `ProactiveSourceSpec` 已编译成真实源并进入 `SourceRegistry`
(`cli._build_source_registry`)。编译、注册与插件声明收集已实弹验过(见
[design/live-verification.md](./design/live-verification.md) 第 5 节),但用的是替身 gateway,
没有真实 MCP server 的 fetch/ack 证据。

**接手点**:用一个真实 MCP server 声明一个源,验证 fetch/ack 全链路。

**验收**:真实源产出的事件进入三通道去重、被判断链路消费,ACK 回到源端。

## 5. 真实模型的 JSON 契约面

**现状**:`post_response_worker` 有两处要求模型返回裸 JSON 数组。真实对话验证时发现
deepseek 会返回 `{"intent": []}`,已由 `coremem/compat_worker.py` 在 kirakira 边界容错
(镜像文件保持与 Reference 逐字节一致)。

`ProactiveJudge` 的严格 JSON 与 `finish_drift` 参数解析已在真实模型下跑过(见
[design/live-verification.md](./design/live-verification.md) 第 4、5 节),未出现同类偏差。

**接手点**:仍未覆盖的是**换模型**之后的回归——不同 provider 的 JSON 顺从度不同,
换 provider 时这三处解析都要重验。

**验收**:换 provider 后连续跑若干轮不因格式偏差抛异常;偏差被记录而不是被猜测吞掉。

## 6. 其余已知差距(未排期)

| 项 | 状态 |
| --- | --- |
| QQ 两渠道逐字节对齐 Reference(Telegram 已对齐) | 未做 |
| 插件包元数据 manifest、非 git 源解析、独立 doctor 模块 | 未做 |
| 前端 Dashboard、peer-agent、eval | 未做 |
| 主动多目标调度(当前单 `[proactive.target]`) | 未做 |

## 7. agent_restart 与准入协调器

**现状**:控制面已落地(见 [design/control-plane.md](./design/control-plane.md)),
但 Reference 的 `agent_restart` 工具与配套的 `RestartCoordinator` 未移植——
移植控制面时把 `quiesce_for_restart` / `resume_after_restart_cancel` 一并删掉了,
因为没有调用方就是死代码。

**接手点**:补 `agent_restart` 工具时,同时恢复 `ConversationRuntime` 的准入冻结
(caller 必须是唯一在途 turn 才允许冻结),并把 supervisor 的私有重启管道接上。

**验收**:agent 能自己请求换代重启,重启期间不接受新 turn,在途 turn 跑完才退出。

## 8. 仍未实弹验证的边界

代码已完成、但尚未在真实环境跑过的项(渠道、崩溃恢复、长跑等)统一记在
[design/live-verification.md](./design/live-verification.md) 第 8 节,本文不重复。
