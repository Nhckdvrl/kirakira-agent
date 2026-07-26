# 控制面:让运行中的 agent 可被观测与驱动

- 状态:accepted;2026-07-26 落地并实弹验证
- 核对基线:`Reference/agent/control/` + `Reference/infra/control/` @ `012e37c8`
- 关联:[NOW.md](../NOW.md)、[live-verification.md](./live-verification.md)

## 1. 解决什么问题

在此之前,一个跑着的 kirakira 只能通过渠道(Telegram/Web)对话,或者看日志。
**没有任何程序化入口**可以问"你现在在忙什么"、"把这一轮掐掉"、"跑一轮但别发到群里"。
这是"能跑"和"能运维"的分界线:没有控制面,一切干预都要重启进程。

控制面提供的正是这条缝:workspace 私有 Unix socket 上的 JSON-RPC 2.0,
**与渠道链路完全并行**,不抢 session、不产生出站消息。

## 2. 分层

照 Reference 的四层,每层只做一件事:

```text
socket.py        SocketAppServer     监听、连接数上限、0600 权限、stale socket 清理
connection.py    NdjsonConnection    一条连接:NDJSON 分帧、有界写队列、背压
protocol/        ConnectionRouter    JSON-RPC 信封校验、方法分发、错误码映射
service.py       ControlService      协议方法 → 领域操作的投影
runtime.py       ConversationRuntime turn 排队/执行/中断/事件流(领域核心)
store.py         ControlStore        turn 状态机的持久化 owner(SQLite CAS)
binding.py       (kirakira 特有)     把控制面接到 PassiveTurnPipeline
```

关键是 `ConversationRuntime` **不知道 agent 怎么跑**——它只拿到一个
`TurnExecutor`。`binding.py` 提供那个 executor,把控制面 turn 投影成一次
`pipeline.run(..., dispatch_outbound=False)`。

## 3. turn 状态机

```text
queued ──→ in_progress ──→ completed
   │            ├────────→ interrupted   (用户显式中断)
   │            ├────────→ failed        (必须带 error)
   └────────────┴────────→ cancelled
```

三条不可协商的规则(与 Reference 逐条对齐):

1. **创建时必须是 queued**,且不得带 `started_at`/`usage`/`error`/`final_response`;
2. **每次转换是一条 SQL 的 compare-and-set**,`WHERE id=? AND status=?`(可选带
   `session_key`)。`rowcount != 1` 就回滚并报错,绝不"覆盖式写入";
3. **failed 必须带 error**,否则拒绝转换——不允许出现"失败了但不知道为什么"的记录。

终态只写一次:`_run` 的 `finally` 是唯一的结束通知点,结果 future、事件流收束、
thread owner 释放三件事在同一处完成。

## 4. 事件流与慢消费者

一个 turn 的事件按序发布:

```text
turn/queued → item/started(user) → item/completed(user) → turn/started
  → [item/started → …delta… → item/completed]*
  → turn/completed
```

订阅者各持一个有界队列。**队列满时不阻塞发布方**,而是清空该队列、塞进一个
`SlowConsumerError` 并把它踢出订阅集——慢客户端只会毒死自己,不会拖慢 turn。
这条是 Reference 的设计,`_publish` 里那段"清空 + 投毒 + discard"就是它。

## 5. 与被动链路的关系

| | 渠道 turn | 控制面 turn |
| --- | --- | --- |
| 入口 | MessageBus | ConversationRuntime |
| session key | `telegram:123` | `programmatic:<uuid>` |
| 串行保证 | AgentLoop 按 session | runtime 按 thread(每 thread 至多一个 active) |
| 出站 | 发回原渠道 | 只回给调用方,不产生 OutboundMessage |

两者的 id 命名空间天然不重叠,因此不会落到同一个 session 上。
`thread/list` 会把渠道会话也列出来(它们就是 session),但 `turn/start` 只对
控制面自己创建的 thread 有意义。

## 6. 参数校验:唯一一处有意偏离

Reference 用 pydantic 的 `StrictModel`(`extra="forbid"` + `strict=True`)。
kirakira 全项目没有 pydantic 依赖(工具 schema 也是手写 `object_schema`),
为一个文件引入它不划算,因此 `protocol/models.py` 手写了等价校验器。

**保持一致的**:方法名、字段名、默认值、长度/范围约束、拒绝未知字段、
不做类型强转(`True` 不能当 `1`)、错误 data 里的 `issues` 形状。
**不同的**只有校验器实现本身。

## 7. 认证与暴露面

- 默认监听 `<workspace>/.kirakira/control.sock`,绑定后立刻 `chmod 0600`;
  chmod 失败会关掉 server 并删除 socket,**不留下一个权限不明的入口**。
- 发现已有 socket 时先尝试连接:连得上说明有活跃 owner,**报错退出**而不是顶掉它;
  连不上才认为是 stale 并删除。
- `KIRAKIRA_CONTROL_ENDPOINT` 可改成 loopback TCP(`127.0.0.1:PORT`),
  非 loopback 地址直接拒绝。
- `KIRAKIRA_CONTROL_TOKEN` 配置后,`initialize` 必须带匹配的 `workspaceToken`,
  用 `secrets.compare_digest` 比对。

## 8. 已知粗糙处

`plugin/disable-and-drain` 传入不存在的插件 id 时,`ValueError` 落到 router 的
通用兜底,返回 `-32603 Internal error` 而不是更准确的参数错误。**这与 Reference
行为一致**(它的 router 同样没有特判),因此保留;要改的话应该连同 Reference 的
`NOT_SUPPORTED = -32013` 一起重新设计错误映射,不适合单点打补丁。

## 9. 验收

离线:`tests/test_control_plane.py` 17 个用例覆盖状态机、CAS、事件序、中断、
thread busy、协议严格性、socket 权限与 owner 冲突。

实弹(2026-07-26,真实 deepseek):见
[live-verification.md](./live-verification.md) 第 8 节。

---

## agent_restart:Agent 自请求换代重启

重启协调器绑定的正是本文这个唯一 `ConversationRuntime`(准入冻结靠它),
所以两者写在一处。状态:**真实 supervisor 换代已实弹通过**(2026-07-26,
见 [live-verification.md](./live-verification.md) 第 9 节)。

### 1. 问题

kirakira 的 `agent/supervisor.py` 与 Reference 逐字节一致,它自始至终在等一个握手:
子进程向继承的私有管道写**恰好一帧**带本 boot nonce 的 `restart_commit`,然后以退出码
75 退出——supervisor 校验通过才拉起下一代。**F**

但 kirakira 进程内此前没有任何产生 75 的路径:`_valid_commit` 是永不触发的死代码,
supervisor 只是个 crash 监管器。控制面移植时删掉了 `quiesce_for_restart` /
`resume_after_restart_cancel`(当时没有调用方,按纪律不保留死代码);本轮补上
`agent_restart` 工具后一并恢复。**F**

### 2. 调用链与状态 owner

```text
supervisor(Reference 原文)                        gateway 进程
  spawn(env: AKASHIC_SUPERVISED/BOOT_ID/            │
        RESTART_COMMIT_FD/RESTART_NONCE)            ▼
  ──────────────────────────────────▶  cli.build_runtime
                                         SupervisorCommitChannel.from_environment()
                                         RestartCoordinator(boot_id, commit=channel.commit)
                                         build_control_plane(restart_coordinator=…)
                                           └ ConversationRuntime(restart_coordinator=…)
                                           └ bind_admission(quiesce=…, resume=…)
                                         register_agent_restart_tool(deferred)
                                                    │
  控制面 turn:模型 tool_search 发现并调用 agent_restart(reason)
        │
        ▼
  coordinator.arm(turn_id=current_turn_id.get(), …)
        │  ① caller 必须是 ConversationRuntime 唯一在途 turn(查 _tasks)
        │  ② 冻结准入:_accepting_turns=False;新 start_turn 抛 RuntimeClosedError
        │  ③ 起 15s delivery watchdog
        ▼
  turn 正式落终态 ──▶ ConversationRuntime._run finally: mark_turn_terminal
  终态事件送达客户端 ──▶ router → ControlService.notify_turn_delivered → mark_delivered
        │  两个条件都成立才 commit(顺序无关)
        ▼
  channel.commit(request):单次 os.write 一帧 JSON+\n(< PIPE_BUF,原子)
        ▼
  runtime_serve 的 restart_task 完成 → stop_background → 返回 75 → sys.exit(75)
        ▼
  supervisor:_valid_commit ✓ + readiness ✓ → 拉起下一代
```

状态 owner:重启状态机(IDLE→ARMED→WAITING_DELIVERY→COMMITTED/CANCELLED)由
`RestartCoordinator` 独占;准入布尔由 `ConversationRuntime` 独占;coordinator 只经
`bind_admission` 注入的两个回调触碰它。**F**

### 3. 失败、取消与并发

| 情况 | 行为 |
| --- | --- |
| caller 不是唯一在途 turn | `quiesce_for_restart` 抛 `RuntimeClosedError`,arm 失败,工具返回错误,不冻结 |
| 非 supervisor 托管 | `arm` 抛 `RestartRejectedError("未由 supervisor 托管")`;工具本身不注册 |
| 渠道 turn 调用(无 current_turn_id) | `arm` 拒绝"缺少完整 turn 上下文"——**快速失败**,不是等 watchdog(见 NOW 1.2) |
| caller turn 非 completed 终态 | `mark_turn_terminal` → cancel → 恢复准入 |
| 终态事件送达失败 | `notify_turn_delivery_failed` → cancel → 恢复准入 |
| 15s 内没有送达确认 | watchdog → cancel → 恢复准入 |
| commit 写管道失败/短写 | cancel → 恢复准入,supervisor 看不到有效帧,不换代 |
| 同 caller 重复调用 | 幂等返回原 request;其他 caller 拒绝 |
| supervised 但缺 commit fd | `from_environment` 抛 RuntimeError,进程启动失败(环境契约破坏,fail loud) |

### 4. 与 Reference 的偏离(显式)

1. **`from_environment` 双读 `AKASHIC_*` 与 `KIRAKIRA_*`**——与 cli 的 readiness
   识别同口径;supervisor 是 Reference 原文,实际设的是 `AKASHIC_*`。
2. **工具注册用 `deferred=True` 近似 Reference 的 `requires_turn_search`**——kirakira
   registry 没有单轮授权域;deferred 保证它不进默认工具面,模型必须 tool_search 发现。
   授权粒度弱于 Reference(发现后本 session 可复用),记为已知差(NOW 1.6 元数据层)。
3. **渠道 turn 快速拒绝**而非 Reference 的投递观察者路径(NOW 1.2)。

### 5. 验收

- 离线(已过,`tests/test_agent_restart.py`,11 例):双条件才提交、失败/超时恢复准入、
  唯一 turn 准入、幂等/拒绝、**提交帧通过字节对齐 supervisor 的 `_valid_commit`**、
  ConversationRuntime 端到端(executor 内 arm→终态→送达→committed;冻结期间新 turn 被拒;
  取消后恢复)。
- 实弹(F,2026-07-26):真实 supervisor + 真实 deepseek 换代一次成功——模型自己
  tool_search 发现工具并调用,回复送达后 gateway 以 75 退出,supervisor 校验通过拉起
  第二代(新 bootId/pid),第二代正常服务 turn。证据见
  [live-verification.md](./live-verification.md) 第 9 节。
- 仍是 G:换代冻结窗口内并发连接的 retryable 拒绝观察(NOW.md 1.1)。
