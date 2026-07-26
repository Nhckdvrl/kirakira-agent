# agent_restart:Agent 自请求换代重启

- 状态:accepted;进程内链路已实现,**真实 supervisor 换代已实弹通过**(2026-07-26,见 [live-verification.md](./live-verification.md) 第 9 节)
- 核对基线:`Reference/` @ `012e37c8b51df045353972bb551d8e868ab52455`
- 核对日期:2026-07-26
- 目标读者:维护者、做实弹验证的人
- 关联:[NOW.md](../NOW.md) 1.1/1.2、[live-verification.md](./live-verification.md) 第 8 节

标注:**F** 已从代码确认;**G** 未验证。

## 1. 问题

kirakira 的 `agent/supervisor.py` 与 Reference 逐字节一致,它自始至终在等一个握手:
子进程向继承的私有管道写**恰好一帧**带本 boot nonce 的 `restart_commit`,然后以退出码
75 退出——supervisor 校验通过才拉起下一代。**F**

但 kirakira 进程内此前没有任何产生 75 的路径:`_valid_commit` 是永不触发的死代码,
supervisor 只是个 crash 监管器。控制面移植时删掉了 `quiesce_for_restart` /
`resume_after_restart_cancel`(当时没有调用方,按纪律不保留死代码);本轮补上
`agent_restart` 工具后一并恢复。**F**

## 2. 调用链与状态 owner

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

## 3. 失败、取消与并发

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

## 4. 与 Reference 的偏离(显式)

1. **`from_environment` 双读 `AKASHIC_*` 与 `KIRAKIRA_*`**——与 cli 的 readiness
   识别同口径;supervisor 是 Reference 原文,实际设的是 `AKASHIC_*`。
2. **工具注册用 `deferred=True` 近似 Reference 的 `requires_turn_search`**——kirakira
   registry 没有单轮授权域;deferred 保证它不进默认工具面,模型必须 tool_search 发现。
   授权粒度弱于 Reference(发现后本 session 可复用),记为已知差(NOW 1.6 元数据层)。
3. **渠道 turn 快速拒绝**而非 Reference 的投递观察者路径(NOW 1.2)。

## 5. 验收

- 离线(已过,`tests/test_agent_restart.py`,11 例):双条件才提交、失败/超时恢复准入、
  唯一 turn 准入、幂等/拒绝、**提交帧通过字节对齐 supervisor 的 `_valid_commit`**、
  ConversationRuntime 端到端(executor 内 arm→终态→送达→committed;冻结期间新 turn 被拒;
  取消后恢复)。
- 实弹(F,2026-07-26):真实 supervisor + 真实 deepseek 换代一次成功——模型自己
  tool_search 发现工具并调用,回复送达后 gateway 以 75 退出,supervisor 校验通过拉起
  第二代(新 bootId/pid),第二代正常服务 turn。证据见
  [live-verification.md](./live-verification.md) 第 9 节。
- 仍是 G:换代冻结窗口内并发连接的 retryable 拒绝观察(NOW.md 1.1)。
