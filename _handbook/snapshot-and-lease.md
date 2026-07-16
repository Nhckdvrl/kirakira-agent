# 运行时快照与租约

## 先理解它是什么

快照系统解决一个问题：**一个 turn 可能跑几十秒，这期间热重载可能把它正在用的工具换掉。**

简单说：**turn 开始时锁定一份能力集合，跑完为止都用这一份。**

- **什么时候用到**：每个被动 turn 开始时自动取一份租约，结束时自动释放。
- **锁定了什么**：MCP 工具、插件 phase 模块、tool hooks。
- **没锁什么**：基础 ToolRegistry 里的内置工具（它们启动即固定，不会变）。
- **跟直接用 ToolRegistry 的区别**：注册表是共享可变的，快照是不可变代际。

```text
RuntimeSnapshotStore
├─ current              新 turn 用哪一代
├─ publish/commit       换代事务：候选先就绪 → 切 current → commit（失败可 rollback）
└─ retire + drain       旧代际退休后，等最后一个租约释放才销毁资源

RuntimeSnapshotLease
└─ 一个 turn 一份；lease_count 归零才允许 drain
```

## 为什么需要它

没有快照时的竞态：

```text
t0  turn 开始，模型准备调用 mcp_fitbit__today
t1  你改了 fitbit.toml，watcher 从注册表卸掉旧工具、断开旧进程
t2  模型真的发出 mcp_fitbit__today
    → "Unknown tool"，或者拿着已断开的连接去调用
```

加锁不行：不能让"改个配置文件"阻塞在一个跑了 5 分钟的 turn 上，高并发下也可能永远等不到窗口。

所以换代**只切 `current` 指针**，不动任何在途 turn 看到的东西。

## 状态机

```text
compiled ──publish──> published ──(新代际发布)──> retired ──(lease_count==0)──> drained
                          │
                          └──rollback──> retired ──> drained
```

| 状态 | 含义 |
| --- | --- |
| `compiled` | 候选已编译，还没生效。只有 compiled 能被 publish。 |
| `published` | 就是 current，新 turn 会拿到它。只有 published 能被 lease。 |
| `retired` | 不再接新租约，但在途 turn 还在用。 |
| `drained` | 最后一个租约已释放，资源（MCP 进程）已销毁。 |

## 核心约束

1. **一个 turn 一份租约，整轮不变**：`SnapshotToolView` 在 reasoner 每轮开头解析一次，
   之后整轮都用它。不要在 turn 中途重新读 `current`。
2. **子任务必须 `fork()` 自己的租约**：ContextVar 会被 `asyncio.create_task()` 自动继承，
   所以子任务能"免费"看到父任务的快照——但它没有租约。父 turn 一结束，计数归零，资源就被
   销毁了，子任务会拿着一个 drained 的快照继续跑。
   `get_current_runtime_snapshot()` 校验 `owner_task` 正是为了让这种白嫖**返回 None 而不是
   一个随时会消失的快照**。想跨任务用，就 `lease.fork()`。
3. **`lease_count` 是资源回收的唯一依据**：任何绕过租约拿到快照的做法都会让计数说谎，
   而计数说谎 = 资源被提前回收。
4. **换代不动在途 turn**：publish 只改 `current`。如果你发现自己在换代逻辑里遍历修改
   已发布的快照，说明设计错了。
5. **同一时刻只有一个发布事务**：`publish` 时已有 pending 事务会直接抛错，不排队。
6. **会变的东西挂快照，不变的挂注册表**：MCP 工具挂快照；内置工具挂基础 ToolRegistry。
   往共享注册表里注册"会被热替换的东西"，就是把竞态请回来了。

## 谁持有租约

| 场景 | 租约 |
| --- | --- |
| 被动 turn | `PassiveTurnPipeline.run()` 自动取，`finally` 里释放 |
| 没有 store（测试/精简装配） | 不取租约，`SnapshotToolView` 退化为只看基础注册表 |
| REPL `/tools` | 不取租约，直接读 `current` 只为展示 |
| 后台子任务 | **必须自己 fork**，见约束 2 |

## 资源回收顺序

```text
watcher 发布新代际
  → store.publish(candidate) 切 current
  → store.commit(transaction)
  → 旧快照 state = retired
  → 旧快照 lease_count 归零？
      是 → drain：调 on_drain → McpCatalogPublisher.drain_snapshot → 断开旧 MCP 进程
      否 → 等最后一个在途 turn 结束后再 drain
```

**先发布新的，再回收旧的**，中间没有窗口期。

## 失败会怎样

| 情况 | 结果 |
| --- | --- |
| 候选连接失败 | 候选整批断开，`current` 不变，异常抛给调用方 |
| publish 后 commit 失败 | rollback 回旧快照，候选自己的资源被 drain |
| 释放次数多于获取次数 | 抛 `RuntimeError`（计数失衡必须暴露，不能静默归零） |
| 重复 release 同一个 lease | 幂等，第二次是 no-op |

## 排查

`store.current.snapshot_id` 是内容派生的稳定 id；`mcp_generation_id` 对应一批 MCP 连接。
`watcher.status()` 会同时给出两者。如果一个 turn 行为反常，先确认它锁的是哪一代——
**"全局已经是新代际"和"这个 turn 用的是旧代际"可以同时为真，这是设计，不是 bug。**
