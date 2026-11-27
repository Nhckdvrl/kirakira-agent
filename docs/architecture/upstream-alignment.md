# 跟进上游：功能对齐清单

本 fork 锁在上游 `e4ba3e29`（2026-10-21），上游 HEAD 是 `08441729`（2026-11-27），中间 **562 个提交**。这一页只做一件事：把"差哪些功能"写成可核对的清单，而不是凭印象说落后。

## 怎么量的

功能的单位取**模型可调用的工具名**——对一个 agent 来说，这是最接近"能做什么"的东西。三棵树各自做 AST 扫描，取 `catalog.register(name=...)`：

```sh
python3 scripts/diff_upstream_tools.py <upstream-core> <upstream-extra>
```

工具的判定条件是 `register(...)` 调用里带 `parameters=`——那是给模型看的 schema。**第一版没加这个条件，数错了**：`register(name=...)` 同样用于上下文材料和 UI 插槽，于是上游的 `session-handoff`（一条上下文材料）被当成"缺一个工具"。修正后：

| | 工具数 |
| --- | ---: |
| 本 fork | 19 |
| 上游 core | 7 |
| 上游 extra | 21 |
| 上游合计 | 28 |

补齐 `subagent_read` 与 `subagent_send` 之后，上游有而我们没有的是 **13 项**，其中没有一项是基本功能缺口（逐项见下）。

上游把插件拆成了 core / extra 两个仓库（ADR 0167），所以必须两边一起比，只比 core 会得出"上游功能变少了"的错误结论。

## 上游有、我们没有（13 项）

| 工具 | 在哪 | 判断 |
| --- | --- | --- |
| `subagent_send` | extra / subagent | **已补齐**（见下） |
| ~~`session-handoff`~~ | core / conversation | **不是工具**：它是一条上下文材料（`MATERIALS_V4`），数据来自 `SessionOperations`，而那整套挂在上游的 Operation Bus（`OPERATIONS_V1`）上。单独做一个"交接材料"而没有交接机制，等于一条永远不触发的提醒。归入 Operation Bus 一并评估 |
| `self` | core / self_cli | 上游新架构（Operation Bus），不属于"基本功能"，单独评估 |
| `codemode` | core / codemode | 单独评估 |
| `fetch_messages` | extra / akasha | **不是缺口**：我们有 `read_memory_source` / `search_memory_source`，且准入更严（按回执而非按范围），见 [fork 对比](fork-vs-upstream.md) |
| `watch` | extra / watch | **刻意不跟**：上游用它取代 Wake（ADR 0149），我们保留 Wake + followups |
| `open_document` | extra / file_viewer | 可选集成 |
| `design_show_html` | extra / design | 可选集成 |
| `phone_observe`、`phone_action`、`phone_shell`、`phone_file`、`phone_devices`、`phone_events`、`device_subscribe` | extra / phone_use | 可选硬件集成，7 个 |
| `roll_dice` | core / skills 模板 | **不是功能**：插件开发脚手架里的示例 |

## 我们有、上游没有

| 工具 | 说明 |
| --- | --- |
| `read_memory_source`、`search_memory_source` | 按回执准入的原文读取，是这个 fork 的差异点 |
| `show_site` | `plugins/sites` |
| `native_probe` | 测试夹具，不是功能 |

## 已补齐：`subagent_read`

**缺口是什么**：父 Agent 派出子任务后，只能用 `spawn_manage` 看**状态**和**最终结果**（`status` / `result`），读不到子任务**一步步做了什么**。上游有专门的只读工具。这个缺口正好落在[我们明确要维持的架构](why-not-off-the-shelf.md#五为什么不是superagent-做入口--一堆子-react-agent)上——主 Agent 与子会话的关系如果只有"派出去、等结果"，那"子任务交回的是同一个账本里的一轮"这个说法就只剩一半。

**补法**：`Subagents.read_child_messages` + `plugins/subagent/tools.py::Read`，注册为 `subagent_read`。

- 父会话归属检查与 `spawn_manage` **共用同一条**（`request.parent_session_id != parent_session_id` → `PermissionError`），所以填别人的 `job_id` 读不到东西，而不是读到别的会话的过程。
- 每行带出处（`session_id` / `message_id` / `seq` / `recorded_at` / `body_kind`），正文按 `length` 截断并给出 `source_chars` 和 `truncated`，所以读到的每一行都能回源核对。
- `after_seq` 增量续读；默认读最近一段。
- Control 行返回它的 `reason`，否则被取消的子任务读起来像什么都没做。

**回归**：扩在 `tests/test_background_jobs.py::test_native_subagent_status_and_original_result_remain_scoped_after_restart` 里，复用它已有的夹具——那个夹具本来就种了一个属于另一个父会话、正文为 `SECRET_OTHER` 的任务。断言包括跨会话被拒且正文不出现、未知 job 报错而不是空成功、截断与 `source_chars` 一致、`after_seq` 续读只给后续行、取消原因可读。

突变双向验证：去掉归属检查，测试在第 434 行失败；去掉截断，在第 421 行失败。

## 已补齐：`subagent_send` 与它背后的插话原语

上游的 `subagent_send` 做两件事：运行中**排队插话**，已结束则**沿用原上下文继续执行**。

**比预期便宜，因为一半已经在了**：我们的 `ReplyExecuteV4` 本来就有 `before_step`（"每一步模型请求之前调用；对话来源用它写入用户的插话"）。缺的只是子任务这一侧的队列和结算互斥。

实现（`plugins/subagent/runtime.py`）：

- `_steers` 队列 + **一把 `asyncio.Lock`**。排队插话和结算必须互斥，否则一条正好在回合结束瞬间到达的插话会被丢掉，而父 Agent 已经收到 `queued`、以为它送达了。**静默丢消息比拒绝更糟**，所以这把锁是这次改动里最要紧的一行。
- `_drain_steers` 把排队文本写成子会话的 `Input`（`author="parent"`，同一个账本），并把 `task.boundary_hint` 推到最新一条。写进账本才算送达，所以插话和子任务自己的消息一样能被 `subagent_read` 读到、回源核对。
- `steer_before_step` 交给 `REPLY_EXECUTE` 的 `before_step`。
- `_drain_or_finish` 在锁里二选一：还有插话就接着跑，没有就结算。**用户取消优先**，取消之后排队的插话一律丢弃。

**还改了终态判定**，这是让"已结束的子任务能被重开"成立的关键。`_terminal` 原来取"最后一条终态"，现在反向扫描、**遇到同来源的 `Input` 就停**：父 Agent 的消息写在上一条 `Output` 之后，于是这一轮重新变成未结束，程序接着跑，`inspect` 也把它报成 pending 而不是 completed。没有插话时与原逻辑完全一致——子会话里在终态之后本来不会再出现同来源的 `Input`，既有的 522 项回归全部通过正是这一点的证据。

**没有照搬的一件事**：上游还有 `retry_wake`，一个让退避等待中的步骤被插话唤醒的 Event。我们的 `REPLY_EXECUTE` 没有这个参数，而它只影响插话多快被看到（下一步边界 vs 立刻），不影响是否送达。**所以没有为它改核心循环，也没有加一个记而不用的 Event。**

**回归**：`tests/test_background_jobs.py::test_parent_interjection_is_queued_or_reopens_and_is_never_silently_dropped`。覆盖排队、落账与出处、重开后 `outcome` 变 None 且状态回到 pending、回合末竞态、取消优先、跨会话被拒且对方账本无写入、空消息被拒、无真实调用消息被拒。

突变四向验证，全部按文件行号失败：去掉回合末排空（静默丢消息）、让取消也排空、去掉父会话归属检查、让 `_terminal` 忽略更晚的 `Input`。

## 架构对齐：Operation Bus

工具面补齐之后，剩下的落后不在"少几个工具"，而在**设计逻辑**。上游 11 月做的
Self-CLI Operation Bus（`plugins/operations`，提交 `0af31c5c`）是其中最值得采纳的一条，
已移植。

**它解决什么**：一个领域动作过去可能有两条路径——界面一条 RPC，Agent 一条工具——各自
改同一份状态。总线把"谁拥有这个动作"收敛成一处 `OperationSpec`，用户和 Agent 用同一个
`actor` 参数走同一个 handler。

**为什么这条特别值得我们采纳**：本项目的主张是"每次取用都说得出是谁要的"。如果界面和
Agent 各走私有路径去改状态，那句话在写操作上就是空的。而 `mirrors` 把"界面入口 ↔ Agent
可调用操作"的一对一关系变成**可核对的声明**，不是一句说法。

已移植（`plugins/operations/`）：

| | |
| --- | --- |
| `contract.py` | `OperationSpec` / `OptionSpec` / `ExtensionSpec` / `OperationRequest` / `OperationResult`、`OPERATIONS_V1`、`cli_node_key`、`register_query_mirrors` |
| `registry.py` | 注册、正交扩展解析、`describe` / `render_help`、argv 解析与类型强制、幂等执行与回执 |
| `plugin.py` | 发布 `OPERATIONS_V1`；只依赖 ledger 契约，处在依赖图底层，业务插件可软挂载而不成环 |

两条设计细节值得单独记，因为它们是这条总线比"各写一个 handler"强的地方：

- **回执绑定调用指纹**（actor、session、argv）。同一个 `request_id` 换了身份或换了参数
  是 `conflict`，不是"拿到别人的结果"；进行中的同一调用共享同一个 future，所以重试不会
  把副作用做第二遍。
- **handler 在注册者自己的作用域里运行**（`context.runtime_scope()`）。能力借用和 owner
  身份不经总线转手，总线只做路由。

**与上游的一处明确差异**：上游的 `ExtensionSpec` 还有 `stage`，让多个 owner 在同一个
联合事务里各自交出写入（上游为此在 ledger 加了 `OwnerWrite` 与多 owner 联合事务，提交
`928478b0`）。**我们的 ledger 没有那个原语，所以没有移植 `stage`**——留一个不能真正原子
提交的联合写入口，比没有更危险。扩展只有 `validate` 和 `apply`。

**回归**：`tests/test_operations_bus.py`，8 项。覆盖一路径一 owner、路径合法性、卸载后
路由消失、参数校验与类型强制（且 JSON 解析失败不回显原值，它可能是凭据）、回执归属与
并发重试只执行一次、`user_only` 对 Agent 既不可见也不可达、正交扩展命名空间冲突与校验
短路、handler 异常如实回报。突变四向验证，全部按测试名失败。

### `self_cli`：两条入口在这里汇合

总线本身没有使用者就是个空底座，所以同时移植了 `plugins/self_cli`（上游同名插件）。
它和 `operations` 分成两个插件，是为了避免总线依赖工具目录而成环。

| 入口 | actor |
| --- | --- |
| 模型工具 `self`（argv 数组） | `agent` |
| RPC 方法 `self/invoke` | `user` |

**差别只有 `actor`**，调的是同一个 `OPERATIONS_V1.invoke`。所以 `audience="user_only"`
的操作天然只在用户侧可用，不需要在两套实现里各写一遍权限判断——那正是两条私有路径最
容易写歪的地方。工具调用的 `key` 直接当作总线的 `request_id`，所以同一次工具调用重试
会重放回执，而不是把写操作做第二遍。

### 第一个纵切：工作目录

光有总线和入口还不够，得有真实的领域操作挂上去。`plugins/working_directory` 原来只有
界面能读当前目录（plugin UI 的 `directory.current` → `current_info`），Agent 没有这条
路径。现在同一个 `current_info` 同时服务两边：

- 界面：`ui:working_directory/directory.current`
- Agent：`self ["wd", "current"]` → operation `working_directory.current`

operation 用 `mirrors` 声明它镜像的正是上面那个界面入口。

### 一对一门禁

`scripts/check_operation_parity.py` 读插件源码，守两条规则：

1. **声明的镜像必须真实存在**——某个 operation 说它镜像 `ui:x/y`，而没有界面提供
   `y`，就失败。否则 `mirrors` 会退化成注释，而这条门禁存在的意义恰恰是防这个。
2. **每个界面入口要么已被镜像，要么登记在待办里**（`scripts/operation_parity_manifest.json`）。
   这个集合只能变小：镜像一个就删掉它那行；新增一个未镜像的界面入口会失败，直到它被
   镜像或带理由登记。

还有第三条，堵一个真实的漏洞：**声明的镜像必须属于声明它的那个插件**。否则一个自己
没有 Agent 路径的插件，可以指着邻居的界面入口通过门禁——那什么也没证明。

第四条：**待办里的每一项必须写出理由，不能是空字符串**。"还没排到"和"Agent 永远不该有
这个"是两种不同的状态，一个裸列表分不出来——而分不出来的待办迟早变成"总有一天全镜像"
的隐含承诺。

当前状态：**15 个界面入口，6 个已镜像，9 个在待办且都有理由**。待办一开始就长是故意的——
第一天就要求全量对齐的门禁只能被关掉，而关掉的门禁什么都不检查。

突变五向验证，且**核对了退出码**（不是只看输出）：镜像一个不存在的入口、去掉 `mirrors`、
把已镜像的入口留在待办里、指向别的插件的入口、把某项理由清空，五种都 exit 1，干净时 exit 0。

### 已镜像的六个纵切

| 界面入口 | Agent 入口 | 同一个领域方法 |
| --- | --- | --- |
| `ui:working_directory/directory.current` | `self ["wd","current"]` | `WorkingDirectories.current_info` |
| `ui:skills/skills.list` | `self ["skills","list"]` | `skills.list_skills` |
| `ui:akasha/scope.policy.get` | `self ["memory","scope"]` | `policies.read` / `recall_enabled` |
| `ui:projects/project.list` | `self ["project","list"]` | `Projects.list` |
| `ui:projects/project.directory` | `self ["project","directory"]` | `Projects.directory` |
| `ui:host_execution/bridge.state` | `self ["host","bridge"]` | `HostBridgeStatus.snapshot` |

六个都**直接调领域方法，而不是去调界面那条 query**，所以无界面的 headless profile 也
能用；`mirrors` 负责记录对应关系，由门禁核对。

### 剩下 9 项为什么不镜像

**不是"还没做完"，是三类不同的判断**，逐项写在 `scripts/operation_parity_manifest.json`：

| 类别 | 项 | 理由 |
| --- | --- | --- |
| 写操作，待授权设计 | `scope.policy.set`、`job.stop`、`project.create` / `rename` / `archive` / `bind_directory` | 分别改记忆准入边界、用户交办的任务、会话维度与目录绑定。让 Agent 自助改需要单独设计，不随只读镜像放开 |
| 界面投影，不是能力缺口 | `akasha/recall.turn`、`workspace_changes/turn.message` | 前者按已渲染轮次取召回卡片，而 Agent 访问记忆走 `recall_memory` / `read_memory_source` 且带回执准入；后者是答案下方的改动摘要，Agent 本来就是产生这些改动的一方 |
| 能力重复 | `projects/directory.browse` | Agent 浏览目录走 FILES 与 working_directory 的既有工具 |

**这一节是对"全镜像"这个目标的明确否定**：有些界面入口按设计就不该有 Agent 路径。门禁
因此检查的是"每一项都有判断"，不是"数字归零"。

**尚未移植**：`session-handoff` 及其 `SessionOperations`（挂在上游 Operation Bus 的
会话交接机制上），以及上游 Parity 门禁 v2 里基于运行实例采集的那部分。

## 维护：锁点摘要门禁的修复

跟进过程中发现仓库自己的完整性门禁是红的：`scripts/check_upstream_kernel.py` 校验每个被
改动的上游文件的 `sha256`，而**召回装饰器那次修复之后 `plugins/akasha/plugin.py` 一直没
更新摘要**。补丁产物此前靠手工重新生成，所以一次普通修复就会让门禁留红。

新增 `scripts/regenerate_upstream_patch.py`，两种模式分开，因为风险不同：

- `--update-digests`：记录漂移文件的当前 `sha256`。这是有意改动上游文件之后需要的，也
  正是仓库门禁实际检查的东西。
- `--rebuild`：额外重写补丁产物。**12 个产物里只有 8 个能从锁点源码用 `diff -u` 逐字节
  复现**，其余的生成过程仓库从未记录，重写它们会把几百 KB 改成同义的另一种写法，或者
  静默丢掉没有上游原件的文件的 hunk。所以 `--rebuild` 对复现不出来的产物**默认拒绝**，
  要 `--force` 才动。

必须用 GNU `diff -u` 而不是 `difflib`：原产物是用它生成的，两者选的 hunk 边界不同
（`@@` 行号差一），用 difflib 会重写全部补丁。拿本 fork 没碰过的产物验证过，逐字节复现。

**还修了这个工具自己的一个漏洞。** 第一版只检查"已经有补丁条目"的文件，而锁点里还有
一类文件：在 `files` 里、没有补丁条目，因此被要求与上游逐字节一致。改动这类文件会让门禁
变红而工具报不出来——移植纵切时改 `plugins/working_directory/plugin.py` 就撞上了。现在
两类都报，并有 `--adopt <artifact>` 把这类文件收进一个产物。

另外要尊重 `generated` 允许项：前端构建产物在锁点里有第二个合法摘要，不认它会在干净的
树上报 6 个假阳性。

当前状态：漂移 0 个文件，9/13 产物可复现，`scripts/check_feature_alignment` 通过。

## 明确不跟的部分

- `watch`：上游 ADR 0149 用它取代 Wake 和 Drift，依据是十四天生产数据（语义兴趣分日均 0.03）。我们保留 Wake + followups，理由与代价写在 [fork 对比](fork-vs-upstream.md#主动交互上游删掉了我们留着)。
- `phone_use`（7 项）、`design_show_html`、`open_document`：可选集成。
- `roll_dice`：不是功能，是 `skills/develop-akashic-plugin` 脚手架里的示例工具。
- `phone_use`：可选硬件集成，和本项目的目标无关。

## 复核方式

```sh
# 工具面差异
python3 scripts/diff_upstream_tools.py <upstream-core> <upstream-extra>

# 本树与锁点的一致性（既有工具）
uv run --frozen --no-sync python -m scripts.check_feature_alignment
```
