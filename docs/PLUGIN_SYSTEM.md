# 插件系统

> 上游对照只用于开发审计；运行时、构建和测试不读取本地 `Reference/` checkout。
> 插件 canonical source 位于 `agent/plugins/`，差距见 [NOW.md](./NOW.md)。

## 1. 核心原则:插件只声明,runtime 负责编译与生命周期

插件不自己 new 一个数据源、不自己起后台 task、不自己 spawn 子进程。它用 frozen dataclass
**声明**能提供什么,由 `PluginManager` 收集,再由 runtime 在正确的代际里编译成真实对象。

这条边界决定了热重载是否可能:只有生命周期归 runtime 持有,换代与卸载时资源才能被确定性回收。

## 2. 声明面

`agent/plugins/specs.py` 与 Runtime API v2 `Plugin` 基类。生命周期固定为
`prepare → activate → retire → terminate`；旧插件的 `initialize()` 由默认 `prepare()` 兼容调用：

| 声明 | 编译成什么 | 承载它的运行时 |
| --- | --- | --- |
| `skill_roots` | 被动 Agent skill 目录链接 | `SkillLoader` |
| `drift_skill_roots` | Drift 候选目录（每轮重新扫描） | `DriftRunner` |
| `McpServerSpec` | MCP server 进程与工具 | `McpCatalogPublisher` |
| `ManagedServiceSpec` | 长驻子进程 | `PluginServiceHost` |
| `ProactiveSourceSpec` | 实现 `ProactiveSource` 协议的对象 | `plugins/wake_proactive/mcp_sources.py` → `SourceRegistry` |
| `PluginJobSpec` | 定时/事件触发的后台作业 | `PluginJobHost` |
| 7 个相位 `*_modules` | 相位模块链 | `PassiveTurnPipeline` / `DefaultReasoner` |
| `tool_hooks` / `register_tools` | 工具与前置钩子 | `ToolRegistry` / `ToolExecutor` |
| `static_semantic_checks` / `readiness_semantic_checks` | 代际准入结论 | `GateResult` |

未移植 Reference 的 `MobileUiContribution`:kirakira 没有 mobile 运行时,不引入没有承载的声明。

## 3. 代际与租约

```text
              装载/热重载
                   │
          build_generation()  ── 语义检查 ──► GateResult
                   │                            │
              gate 通过?                    未通过 → 保留旧代际,记入 errors
                   │ 是
                   ▼
        PluginGenerationRegistry.publish()
                   │
      ┌────────────┴────────────┐
   新代际 active            旧代际 retired
                                 │
                        lease_count > 0 ?
                          │            │
                        是 │            │ 否
                          ▼            ▼
                    等在途 turn      drain_quiescible()
                    释放租约          → quiesced,资源回收
```

一次被动 turn 通过 `PluginGenerationRegistry.lease_active()` 持有当时所有活跃代际的租约
(`PassiveTurnPipeline.run`)。turn 期间发生换代时,旧代际只转 `retired`,`can_quiesce`
为 False,不会被销毁;turn 释放租约后才回收。**换代不会抽走在途 turn 正在用的能力。**

`retired` 代际拒绝新租约;重复 release 直接抛错,不静默吞掉。

## 4. 热重载与安装

```text
install / uninstall / enable / disable
        │  _request_reload()
        ▼
  PluginWatcher.wake()  ◄── 也按 interval 轮询 watch_revision()
        │
        ▼
  PluginManager.reconcile_changed()
        ├─ 消失或被 manifest 关掉 → _deactivate_plugin(终止 + 退休代际)
        ├─ 新出现 → _activate_plugin(装载 + 初始化)
        ├─ revision 变化 → 重建候选代际,过 gate 后换代
        └─ 有变化 → 重发 skill 链接 / MCP / 托管服务
```

`watch_revision()` 取插件源码、配置与 manifest 的内容指纹;文件缺失也算一次变化。
watcher 第一次成功扫描一律触发一次 reconcile 建立基线,因此"启动时扫描失败、之后目录恢复"
不会漏掉换代。扫描异常只记录不退出循环；republish 失败的 revision 不会成为新基线，同一内容在
下一轮继续重试。

每次换代写入 `<workspace>/.kirakira/runtime/plugin-reloads.sqlite3`：
`preparing → prepared → validating → commit_started → committed/draining → complete`。重启时，
commit 前候选被丢弃；commit 已开始的事务必须与磁盘 source revision 和 generation 精确一致才恢复。
发布未完成时 `PluginGenerationRegistry.lease_committed()` 暂停新被动 turn 与 proactive tick；skill、
MCP、托管服务全部收敛后才一次开放 admission，失败则保持关闭并等待 watcher 重试。

安装升级用"旧目录挪成 `.backup-` → 原子换入 → 失败回滚 → 成功删备份",不会留下半个插件。

**单插件失败不扩散**:某个插件 gate 未过或构建失败时只保留它自己的旧代际,其余插件照常换代。

## 5. 相位顺序

模块声明 `slot`(唯一)与 `requires`,`agent/lifecycle/phase.py` 拓扑排序决定执行顺序。

- 缺 slot 声明、slot 重复、循环依赖:fail loud;
- 依赖的 slot 不存在:**级联禁用**该模块及其下游,而不是让它带着坏假设跑;
- 内置 slot(`before_turn.` 等前缀)不参与禁用:它们缺依赖是配置问题,不该被静默摘掉;
- 带冒号的 requires 是能力引用(如 `mcp:server`),不参与模块禁用判断。

生效条件与理由见 [decisions/0001](./decisions/0001-plugin-slot-ordering-opt-in.md)。
`PluginManager.doctor()` 的 `phases` 段输出各相位实际执行顺序与依赖边。

## 6. 安全边界

- 安装期**不导入** `plugin.py`,绝不热执行刚下载的代码;插件身份取来源目录名;
- 远程源只接受 HTTPS Git URL;
- 声明里的命令与 cwd 一律经 `safe_child` / `normalize_command_item` 按插件根解析,越界即失败;
- 托管服务的 readiness 端口已被占用时拒绝启动,不接管别人的服务;
- 作业与服务的失败被隔离:单个失败只记日志,不影响其余与主链路。

## 7. 状态与数据

| 对象 | 增加 | 原位更新 | 失效 | 物理删除 | 授权者 |
| --- | --- | --- | --- | --- | --- |
| `.kirakira/plugins/<name>/` | install(原子换入) | 升级时整目录替换 | manifest 置 false | uninstall | 用户/agent 工具 |
| `.kirakira/manifest.toml` | 首次启停时写入 | enable/disable | —— | uninstall 时删条目 | 用户/agent 工具 |
| `.kirakira/plugin-data/<id>/` | 首次装载时创建 | 插件自己写 | —— | **当前不得自动删除**(uninstall 明确保留) | 仅用户手动 |
| `.kirakira/runtime/plugin-reloads.sqlite3` | reload begin | 只追加 phase event + 更新事务终态 | aborted/recovered | 当前不自动清理 | runtime |
| `PluginGeneration` | publish | 不可变 | retire | 租约归零后 quiesce | runtime |

## 8. 内置订阅插件:curated-feeds

`plugin_packages/curated_feeds/` 是通过公开插件运行时加载的可分发内置包，不是核心层写死的
抓取器：

```text
plugin.py
  ├─ McpServerSpec(curated_feeds)
  ├─ ProactiveSourceSpec(subscriptions, content, fetch + ack)
  └─ drift_skill_roots("drift-skills")
       ├─ track-a-share-market / track-japan-market
       ├─ compare-cn-jp-markets
       └─ audit-proactive-signal
```

MCP server 支持 RSS/Atom、WordPress REST collection、网页内容 hash 监控与
阈值/档位化的市场行情；事件 ID
由源 ID 与原始稳定 ID/hash 生成，只对真正投递成功的 ID 做 ACK。抓取快照与
ACK 状态归插件数据目录所有：

```text
<workspace>/.kirakira/plugin-data/curated-feeds/
  ├─ config.local.toml   # 用户订阅配置，最高优先级
  ├─ snapshot.json       # 可重新拉取的稳定快照
  └─ acked.json          # 已投递事件 ID
```

配置合并顺序是插件包 `config.toml` → 旧版兼容的插件根
`config.local.toml` → `plugin-data/<id>/config.local.toml`。私有配置不应写回可分发
插件源码。

当前本地配置同时覆盖 A 股/日经、主要模型公司官方动态、三条 arXiv
主题线、日本 AI/Physical AI 与高信息密度 X 账号。X 使用有序双入口
回退；多 feed 首次刷新使用有上限的并发 I/O，单源失败保留 last-good
snapshot 并在 `errors` 中显式报告。
