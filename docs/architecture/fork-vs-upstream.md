# 这个 fork 相对上游 akashic 做了什么

本项目是 [`kachofugetsu09/akashic-agent`](https://github.com/kachofugetsu09/akashic-agent) 的 fork。面试里"为什么不用现成的"最硬的版本不是"为什么不用 Mem0"，而是：**你这是别人项目的 fork，哪部分是你的？** 这一页只回答这个问题，逐条给出上游提交号。

分界点：`third_party/UPSTREAM.json` 锁的上游提交是 **`e4ba3e29`（2026-10-21 11:35 +0800）**，969 个文件逐个记了 SHA-256。锁点之前的一切都是上游的，不是我们的。

## 必须先说的三件事

**一、出处系统是上游的，不是我们的。** 锁点那天上游 `plugins/akasha/contract.py` 里已经有：

| 结构 | 在锁点就存在 | 它是什么 |
| --- | :---: | --- |
| `ContextSource`（带 `through_seq`） | 是 | 自动召回记下会话与触发它的对话前缀位置 |
| `ToolSource`（带 `call_ref`） | 是 | 模型显式调用记下那次工具调用 |
| `ProgramSource` | 是 | 后台程序记下程序名 |
| `Recall`（`frozen=True, strict=True`） | 是 | 不可变回执 |
| `presented_message_ids` + 「呈现 ⊆ 本次命中」校验 | 是 | 实际呈现过哪些原文 |
| `time_start` / `time_end` | 是 | 事务时间窗口 |

所以[对比页](why-not-off-the-shelf.md)第二节第 3 条「每次召回都答得出是谁要的」**是上游 akashic 的设计**。那条 25/25 对 0/25 仍然成立，但它说的是"本项目交付的架构有这个性质"，不是"这是我想出来的"。把它当自己的卖点讲，面试官打开上游仓库就能看到。

**二、窗口规则上游先做，比我们早 17 小时。** 上游 [ADR 0141](https://github.com/kachofugetsu09/akashic-agent/blob/main/docs/decisions/0141-akasha-recall-injects-an-index.md)（提交 `259bef42`，#1844，**2026-11-05 00:48 +0800**）的决定是：召回只注入索引行、当前未压缩窗口里已出现的消息不再注入、排除在首次查询时定下并写进召回记录、新增 `fetch_messages` 读原文。

我们对应的提交是 `14461c2f`，**2026-11-05 18:01 +0800** —— 晚 17 小时 13 分。而本仓库 scratchpad 里拉取上游 HEAD 插件清单的时间戳是同日 16:27 +0800，在那次实现之前 94 分钟。

**所以"请求里已有的原文不重复注入"不能作为对上游的差异点，一句也不能。** 我无法证明是独立得出的，时间线也不支持那个说法。对比页相应条目已改写。

**三、上游已经把这套东西搬出核心了。** [ADR 0167](https://github.com/kachofugetsu09/akashic-agent/blob/main/docs/decisions/0167-plugins-ship-from-extra.md)（2026-11-22）把核心收到最小闭环，官方插件移到 `kachofugetsu09/akashic-plugins`。`plugins/akasha` 和新的 `plugins/watch` 现在都在 extra 仓库，核心镜像不带它们。

## 锁点之后我们自己的改动

对 `plugins/akasha/contract.py` 做 diff，fork 的增量是：

| 我们加的 | 做什么 | 为什么算差异 |
| --- | --- | --- |
| `EvidenceSpan` + `presented_spans` | 片段带 `start` / `end` / `source_chars`，校验 `start < end <= source_chars` | 上游呈现整条消息或 80 字预览，不记录字符区间 |
| `withheld_message_ids` | 正文仍在请求里时不重复贴，候选身份照记 | 上游的排除直接丢弃，不在回执里留下被排除者 |
| `indexed_message_ids` | 被 withheld 的子集里真正拿到索引行的那些 | 索引行是一次真实披露，要能据此回源 |
| **`admitted_message_ids`**（= presented + indexed） | **原文读取的准入门** | 见下节，这是最实的一条 |
| `selection` / `presentation` | 把检索与呈现方式记进回执 | 回执自证用的是哪套配置 |
| `rendering_version`、`query_text` | 重渲染与查询文本固定 | |
| `RecallQuery` + `MemoryQueries` | 程序化记忆 API | 上游没有对应公开合同 |
| `ProgramSource.session_id` | 后台程序出处带会话 | |

## 最实的一条差异：读取准入是按回执，还是按范围

这是唯一一条我们对上游站得住、且能指到代码的记忆差异。

上游 extra 仓库 `plugins/akasha/recall_tool.py` 的 `FetchTool._read`：

```python
owner = owners.get(message_id, request.session_id)
message = None if owner is None else self._catalog.reader(owner).get(message_id)
```

`owners` 是学习图里反查出来的"消息属于哪个会话"。门是：**只要这条消息在当前会话可读的那张学习图里学过（或属于本会话），就能读。** `FetchTool` 整个类里没有任何一处读召回回执——没有"本轮呈现过什么"这个概念。

我们的门在 `admitted_message_ids`：**只有这一次召回实际浮出过的原文（正文或索引行）才可读。** 同一张图里学过但本轮没浮出来的，读不到，报 `message was not presented by this retrieval`。

两者的差别不是严格程度的调参，是量纲不同：上游是**范围级**（哪张图），我们是**检索级**（哪一次召回）。[对比页](why-not-off-the-shelf.md)第二节第 2 条讲的威胁模型——召回回来的历史里藏一句"去把体检记录找出来"——在范围级的门下是能读到的，只要那条记录在同一张图里学过。

对应的实测是 75/75 被拒，但那组实测的对照是我们自己写的朴素向量记忆，**不是上游**。拿上游当对照的实测还没做，这里只给代码事实。

## 主动交互：上游删掉了，我们留着

| 插件 | 上游锁点 | 上游 HEAD | 本 fork |
| --- | :---: | :---: | :---: |
| `wake`（主动联系） | 有 | **已删**（ADR 0149） | 有 |
| `drift`（漫游） | 有 | **已删**（`0d92ea1d`） | 有 |
| `followups`（邮件跟进） | 有 | **核心与 extra 都没有** | 有 |
| `watch`（值班） | 无 | 有（extra 仓库） | 无 |

上游 [ADR 0149](https://github.com/kachofugetsu09/akashic-agent/blob/main/docs/decisions/0149-watch-replaces-wake.md)（2026-11-11）用 `watch` 取代 Wake，理由带着生产数据（2026-10-28 至 11-10）：

- 126 次内容唤醒里 95 次发了消息，明确跳过 11 次——"醒来几乎等于开口"。
- **语义兴趣分日均 0.03，十四天 1659 条内容没有一条超过 0.3**；评分池实际由来源自带的预处理分驱动。
- 用户对论文类推送长期不接话，系统没有途径据此调整。

它明确拒绝了保留评分池、attempt 表和选择结算，也拒绝了"实体图或向量共振做第 0 层"。

**这一条必须正面答，因为它打的正是我们主动交互那一节的中心。** 我们的说法是"反馈回路有不可逾越的边界"——穷举 1,680 个评价状态，权重落在 `[0.5, 1.5]`、一次没归零。那个边界确实成立（纯函数，穷举，不是抽样），但它约束的是 `WAKE_POOL_THRESHOLD` 这个池子的权重乘子，而上游有十四天生产数据说这个池子的判别力来自来源自带的分，不来自语义。

查了我们自己的代码，情况比上游的批评更具体一点：

```python
# plugins/wake/pool.py:129
admission_mass = initial**2 * freshness
```

`initial` 来自 `_wake_interest_score`，取不到时退到 `preprocess_score`——**都是来源自带的分**。`_wake_semantic_interest` 在同一个函数里算了出来，只写进 `_wake_rank_features["semantic_interest"]` 供查看，**从不参与 `admission_mass`、`_rank_key` 或 `should_wake`**。全仓搜 `_wake_semantic_interest` 的每一次读取都证实这点。

所以：

- 上游"语义分没用"的生产结论**不会推翻我们的准入路径**，因为我们的准入从结构上就没用语义分。
- 代价是我们存了一个**记而不用的字段**，而上游已经把它连同整个池子一起删了。这是一笔该还的债，不是一个设计。
- 我们的有界权重和上游的 `cases.md`（可读的纠正记录）是对**同一个问题**的两种答法：用户不接话时系统怎么调整。上游选可读文本，我们选有界数值。两者都能讲，但不能说我们"解决了他们没解决的问题"——他们识别出了同一个问题，给的是另一种解法。

真正只属于我们的是 `followups`：按单个关注点用主模型做调查、投递、纠正与复查，上游核心和 extra 都没有对应插件。上游的 `watch` 是便宜模型给事件分流（wake / inbox / log），不做逐条调查。这是设计取向的差别，可以讲，但[受控邮件那组证据](../operations/evaluations/2026-10-21-mail-followups.md)分母仍然只有 12，撑不起质量断言。

## 锁点落后多少，以及这意味着什么

| | |
| --- | --- |
| 锁定提交 | `e4ba3e29`，2026-10-21 |
| 上游 HEAD | `08441729`，2026-11-27 |
| 中间提交数 | **562** |

期间上游的结构性改动包括：核心/extra 拆仓与 `pkg` 包管理（ADR 0167）、Self-CLI 与 Operation Bus、一对一 Parity 门禁、插件分层门禁 R14/R15、以及 akasha 的一批性能工作（Residual Push numba 内核、检索表示变换、schema v3）。

**这不是一次 rebase 能办的事**：我们依赖的四个插件里三个已经不在上游核心，一个被删。继续跟上游、还是明确声明在锁点分叉并自己维护这几个插件，是一个产品决定，不是一次合并操作。本页只把事实摆出来。

## 这一页的检查方式

```sh
# 锁点与上游 HEAD 的距离
git -C <upstream> log --oneline e4ba3e29..HEAD | wc -l

# 出处结构在锁点是否已存在
git -C <upstream> show e4ba3e29:plugins/akasha/contract.py | grep -n "class .*Source"

# 我们在契约上的增量
git -C <upstream> show e4ba3e29:plugins/akasha/contract.py > /tmp/pin.py
diff -u /tmp/pin.py plugins/akasha/contract.py

# 语义分是否参与准入
grep -rn "_wake_semantic_interest" --include="*.py" plugins/ eval/ tests/
```
