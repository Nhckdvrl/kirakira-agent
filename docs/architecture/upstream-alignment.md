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
