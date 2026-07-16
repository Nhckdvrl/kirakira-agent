# 记忆系统

## 先理解它是什么

记忆分两件事，别混在一起：

- **Session** = 这次对话说过什么。完整、逐字、只属于一个会话。
- **Memory** = 关于用户的稳定事实。跨会话、经过提炼、要被检索。

简单说：**session 是录音，memory 是笔记。**

```text
┌─ 用户消息
│  └─ 检索：query → 多路召回 → RRF 融合 → 热度加权 → 注入预算 → 塞进 prompt
├─ 回复先返回用户（不阻塞）
└─ 后台 consolidation
   ├─ 达到窗口后调 LLM 抽取结构化 memories/history
   ├─ exact dedup + reinforcement
   └─ 写 items.json 与 MEMORY.md 托管区
```

## 五个 Markdown 文件

都在 `<workspace>/memory/` 下，运行时启动时自动创建：

| 文件 | 写者 | 读方 | 用途 |
| --- | --- | --- | --- |
| `MEMORY.md` | consolidation（托管区） + 人工 | system prompt | 长期记忆：稳定事实、偏好、身份 |
| `SELF.md` | 人工 | system prompt | Agent 的自我模型 |
| `RECENT_CONTEXT.md` | consolidation | system prompt | 近期在聊什么 |
| `HISTORY.md` | consolidation（只追加） | 检索 | 时间线事件日志 |
| `PENDING.md` | 预留 | — | 待整理缓冲 |

`MEMORY.md` 由**托管块 + 人工区**组成。`forget` 只重写托管块，**绝不动人工写的内容**——
否则用户手写的东西会被 agent 悄悄删掉。

结构化记录在 `items.json`：id、type、source_ref、status、reinforcement、时间、可选 embedding。

## 检索：为什么是 RRF 而不是加权求和

两条 lane 各有盲区，所以要融合而不是二选一：

| lane | 擅长 | 盲区 |
| --- | --- | --- |
| `vector` | 口语化、同义改写（"kitten" ≈ "feline"） | `scripts/rollout.sh`、错误码 |
| `lexical` | 变量名、命令、路径、错误码、精确实体 | 换个说法就没了 |

融合**只看名次，不看原始分**：

```text
score(item) = Σ_lane  weight_lane / (k + rank_in_lane)      k = 60, lexical 权重 0.5
```

**为什么不能直接加权原始分**：cosine 在 [-1,1]、词法分在 [0,1]，尺度不可比，相加得到的数
没有意义。更要命的是它让**每条记录都有非零分**，于是 `limit` 永远被无关记忆填满。
RRF 下每条 lane 有自己的准入规则（词法要求 overlap > 0，向量要求 cosine ≥ 0.25），
**不匹配的是缺席，不是排在最后**。

历史细节见 `docs/VERSION_EVOLUTION.md §5.4`。

## 核心约束

1. **写入侧向量失败必须报错，检索侧可以降级**。
   - 检索失败 → 退回词法召回，本轮仍有答案，向量服务恢复后自动变好，**没留下痕迹**。
   - 写入失败 → 这条记忆永远没有向量，索引半有半无，**损坏被固化**。
   所以 `_embed_for_query` 吞异常并降级，`_embed_for_store` 抛错。别把它们合并回一个函数。
2. **回复先返回，consolidation 在后台**。抽取记忆要调 LLM，绝不能挡在用户回复前面。
3. **同 session 下一轮开始前等待上一轮 consolidation 收口**，避免边写边读。
4. **注入有硬预算**（1200 字符 / 单行 180）。检索质量再好也不能吃光上下文。
5. **热度随时间半衰**（alpha 0.20，半衰期 14 天）。否则一条被反复提到的旧记忆会永远压住新记忆。
6. **删除 session 会撤销带 source_ref 的记忆**。不能"对话删了，事实还在"。
7. **去重靠 consolidation 那次 LLM 调用，不靠字符串比对**（见下节）。更正与否定必须能覆盖
   旧事实——去重绝不能把它们压掉。

## 去重：为什么并进 consolidation 那一次调用

两个写入者会撞车：

```text
用户："记住：部署脚本在 scripts/rollout.sh"
  ├─ memorize 工具      → "用户的部署脚本在 scripts/rollout.sh。"        source_ref=:0
  └─ 后台 consolidation → "部署脚本在 scripts/rollout.sh，每次发版都跑它"  source_ref=:0-5
```

`memorize()` 的去重是**精确字符串匹配**，只挡得住**重放**（同文本、同 source_ref），
挡不住**改写**——而 consolidation 的 LLM 必然改写措辞。曾经因此同一事实存两遍，
类型还可能不一致（一条 `event` 一条 `procedure`），注入块一半是冗余。

**为什么不用"词法相似度超阈值就去重"**：实测否决，不安全——

```text
否定句   0.833   CI 跑在 GitHub Actions 上   ||  CI 不跑在 GitHub Actions 上
真重复   0.800   使用的数据库是 PostgreSQL 16 ||  用户使用的数据库是 PostgreSQL 16。
真重复   0.727   错误码 E4011 表示配额超限    ||  用户的错误码 E4011 表示配额超限。
```

**否定句的相似度比真重复还高。** 任何抓得住真重复的阈值都会把「CI 跑」和「CI 不跑」合并掉
再丢一条——让 agent 说反话，比冗余严重得多。去重**必须理解语义**。

**现在的做法**：consolidation 本来就要打一次 LLM，所以把「已记事实」喂进**同一次调用**，
让抽取和去重合并成一次判断——**零额外往返**。`_known_memory_digest()` 取两部分：

- 本 session 已记的（`memorize` 刚写的，最可能被重复抽取）
- 与本轮内容词法相关的旧记忆（跨 session 复述）

prompt 里的规则是显式的，尤其最后一条：

```text
- 只是换个说法表达上面某条事实 → 跳过。
- 需要修正或补充细节 → 输出完整新版本，沿用原 memory_type。
- 与上面某条语义相反（例如否定）→ 必须输出，这是修正，不是重复。
- 只输出上面没有的新信息。
```

倒数第二条是**安全网**：去重绝不能压掉更正，否则 agent 会卡在过时事实上——那比冗余更糟。

reference 用独立的 `memory2/dedup_decider.py`（313 行）多打一次模型来做这件事；我们并进
已有调用，代价更低。

> **注意**：`memorize()` 本身是原语，直接连调两次不同措辞仍会产生两条记录。去重发生在
> consolidation 这一层，不在原语层。

## 失败会怎样

| 情况 | 结果 |
| --- | --- |
| 向量服务挂了，检索时 | 降级为词法召回，记 ERROR 日志，本轮正常回复 |
| 向量服务挂了，写入时 | **抛 RuntimeError**，拒绝写入无法被语义召回的记录 |
| consolidation 抽取失败 | 记 ERROR，用户回复不受影响，下轮重试 |
| `items.json` 损坏 | 启动时暴露，不静默重建 |

## 换一套检索策略

`retrieval.py` 的 `MemoryRetrievalPipeline` 协议就是接缝：

```python
class MemoryRetrievalPipeline(Protocol):
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...
```

被动 turn 依赖协议而非具体实现。要做 query rewrite / HyDE / sufficiency，实现这个协议即可，
主链路不用动。

**但先想清楚**：这三项每一项都要在**每轮对话多打一次 LLM**。纯计算的优化（RRF、热度、预算）
零额外往返，所以先做；要花模型调用的，**先有评测集证明它值，再开**。不要因为"HyDE 听起来
高级"就默认打开。
