# 跟进上游：功能对齐清单

本 fork 锁在上游 `e4ba3e29`（2026-10-21），上游 HEAD 是 `08441729`（2026-11-27），中间 **562 个提交**。这一页只做一件事：把"差哪些功能"写成可核对的清单，而不是凭印象说落后。

## 怎么量的

功能的单位取**模型可调用的工具名**——对一个 agent 来说，这是最接近"能做什么"的东西。三棵树各自做 AST 扫描，取 `catalog.register(name=...)`：

```sh
python3 scripts/diff_upstream_tools.py <upstream-core> <upstream-extra>
```

| | 工具数 |
| --- | ---: |
| 本 fork | 30 |
| 上游 core | 18 |
| 上游 extra | 24 |
| 上游合计 | 42 |

补齐 `subagent_read` 之后，上游有而我们没有的从 17 项降到 **16 项**。

上游把插件拆成了 core / extra 两个仓库（ADR 0167），所以必须两边一起比，只比 core 会得出"上游功能变少了"的错误结论。

## 上游有、我们没有（16 项）

| 工具 | 在哪 | 判断 |
| --- | --- | --- |
| `subagent_send` | extra / subagent | **缺口，下一项**：需要向运行中的子任务注入消息（Steer），我们只有 pause / failure 两种控制 |
| `session-handoff` | core / conversation | 缺口：会话交接 |
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

## 下一项：`subagent_send`

上游的 `subagent_send` 做两件事：运行中**实时 Steer 插话**，已结束则**沿用原上下文继续执行**。我们的 `_control` 只能 pause / failure，没有"往运行中的一轮注入一条消息"这个原语。

所以这一项不是加个工具，是**先要一个 Steer 原语**，它会碰到 react 循环和效果排空。没有和 `subagent_read` 一起做，是因为半成品的注入路径比没有更糟——它会在父 Agent 以为插话成功时静默丢消息。单独排。

## 明确不跟的部分

- `watch`：上游 ADR 0149 用它取代 Wake 和 Drift，依据是十四天生产数据（语义兴趣分日均 0.03）。我们保留 Wake + followups，理由与代价写在 [fork 对比](fork-vs-upstream.md#主动交互上游删掉了我们留着)。
- `self` / `codemode` / Operation Bus / Parity 门禁：上游 11 月的新架构，是否跟进要先评估，不计入"基本功能对齐"。
- `phone_use`：可选硬件集成，和本项目的目标无关。

## 复核方式

```sh
# 工具面差异
python3 scripts/diff_upstream_tools.py <upstream-core> <upstream-extra>

# 本树与锁点的一致性（既有工具）
uv run --frozen --no-sync python -m scripts.check_feature_alignment
```
