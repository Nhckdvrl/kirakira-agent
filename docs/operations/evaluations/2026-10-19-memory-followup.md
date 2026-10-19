# 记忆：公开机制复测、任务质量与检索对照

日期：2026-10-19。所有模型调用只使用 Step5 Token Plan、medium；reader/judge 输出上限 8,192，
最终 Mem0 写入上限 32,768。免费 BGE embedding、真实 Mem0 SDK 与 Agent 共用 `.venv` /
Python 3.14.6，可选依赖由同一 uv.lock 固定。用户真实会话没有参与实验。

本轮修复了一个实质性接线缺口：Kirakira 原 Akasha2 adapter 只返回图 completion，遗漏了
Akashic 宿主的精确向量通道。已补齐双通道，学习算法与原图不变。以下保留旧读出的实测结果，
并分别给出同图诊断、新问题验证与真实外部系统对照。

## 参照与范围

Akashic `d85b00c` 已退役 LongMemEval/PersonaMem CLI。当前公开材料重点是
[机制与成本](https://github.com/kachofugetsu09/akashic-agent/blob/d85b00c233686b68b082052b1b20123905aeab13/docs/design/akasha-memory-cost.md)
与[公开合成场景](https://github.com/kachofugetsu09/akashic-agent/blob/d85b00c233686b68b082052b1b20123905aeab13/scripts/akasha_cost_scenario.py)。
独立引擎另有[数学性质与重放验证](https://github.com/kachofugetsu09/akasha-v2-engine/blob/e85dc2c66ff4eb16af118c638d72e7c32f3dc69a/docs/validation.md)。
上游私人经历和 V8 capture 没有公开，不能伪称复测了这些语料或继承其成绩。

结束前核对当天新 main `21b38f0e102d9e1bdc5576725635dce29f135c0c`：新增 41 个提交主要为插件
边界与 UI；本轮固定的十个算法文件、宿主 recalls、cost 脚本与 cost 文档共 13 个文件逐字节未变。
该检查只确认本轮记忆协议仍适用；实验继续保留原 pin。证据见
[upstream-head-check.json](../../../eval/results/2026-10-19-scheduler/upstream-head-check.json)。

本轮对应上游的公开验证逐项执行：状态/重放、查询纯度、增量/完整写入、发布故障、数学性质、
矩阵计算一致性和成本。另使用公开 LongMemEval 比较任务质量；合成性质与真实语义成绩不互相替代。

上游私人 5,619 turns/42 sessions、11 个真实查询及 V8 capture 没有公开，不能复跑。
其“联系有依据、正文是否表达联系、当下有用、呈现负担”的人工检查也依赖私有原文与标注；
本轮没有冒充复现该人工结论。公开 QA 的证据覆盖和同模型 judge 是另外两类指标。

## 公开状态轨迹复测

采用同一 20260929 种子、272 条经历、33 维向量、三个会话、长间隔、remember/forget、部分缺 dense。
Kirakira 的实际 vendored MemoryCycle 与 writer 和上游实际脚本都运行，没有改学习算法。

- 原始 Turn 输入 SHA-256 完全相同。
- 完整学习状态 SHA-256 完全相同：`f38c5de5af9da045de9e3bef06ca772befbee99b4ba8d6ec4bc4d94253fd42e2`。
- 9 个查询的完整 completion 摘要完全相同；不是只比命中条数。
- 连续 writer 与完整 writer 每一步 schema/全表相等；16 次恢复后下一步状态仍与不中断学习一致。
- 成功查询及故障查询不改变状态；file fsync、replace、directory fsync 三处故障只发布完整旧版或新版。
- PYTHONHASHSEED=0/42 的状态及 45 次查询摘要完全相同；45 次是九个 query 各重复五次。

跨驱动的学习状态比较明确排除消费 receipt：上游场景额外写入了一条 skipped receipt。
各自增量/完整 writer 的全表比较仍包含消费状态，没有省略它。
证据见 [parity](../../../eval/results/2026-10-19-scheduler/upstream-parity.json)。

独立引擎固定提交的数值性质 **21 passed**。只改导入路径和三个容器读法，以适配紧凑 array；
Residual Push 下界/误差、连接预算、资源恢复、局部反馈和可塑性不等式均保留。
其 CLI/schema 专属哈希种子测试明确不运行，由上述 Kirakira checkpoint 实验单独验证。
最初容器类型失败与最终报告都保存，不改生产数组或数值规则来迎合旧测试。

对紧凑矩阵读取另跑 **672/672** 个声明形状：24 种维度（1–3,072）×28 种行数（0–513），
包含 256 分块边界与尾部；float32 矩阵/float64 cue 与完整 NumPy 计算逐位相等。
这些是本轮固定形状，不是作者未公开形状集；结论限于记录的 NumPy、BLAS、平台和有限输入。
见 [matvec-shapes.json](../../../eval/results/2026-10-19-scheduler/matvec-shapes.json)。

## 成本

同机串行独立进程、单线程 BLAS，纯 MemoryCycle/快照；不包含分词、远程 embedding、SessionDB
原文装载、宿主排队、模型和渠道。RSS 包含该测量进程已运行过的完整/恢复路径，不能当作生产 Runtime 内存。

| 输入 | 学习 p50/p95 | 查询 p50/p95 | 发布 p50/p95 | 恢复 p50/p95 | 峰值 RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| 272 turns / 33 维 | 15.63/28.44 ms | 16.57/16.87 ms | 14.91/15.45 ms | 32.57/34.01 ms | 71.24 MiB |
| 1,024 turns / 384 维 | 37.86/151.48 ms | 39.34/39.77 ms | 57.22/81.36 ms | 148.29/174.09 ms | 112.89 MiB |

完整学习计算分别 4.53 秒和 56.21 秒。维度与规模同时不同，只是两个工作负载，不能据此拟合复杂度。
每组有 45 次查询、16 次发布/恢复；不是 45 个独立任务或多机统计。
不拿这些纯算法成本与上游私人 5,619-turn、含分词的 API 计时拼成加速比。

公开 QA 的宿主成本另测如下，四个 case worker、预先缓存向量，每图重复五次只读查询；
学习计时含图与对应 checkpoint，排除共用来源准备和 embedding 推理。

| 题组与 adapter | 每题学习 p50/p95 | 查询 p50/p95 | 查询样本 |
| --- | ---: | ---: | ---: |
| 24 题 / v1 | 131.36/149.68 s | 38.43/45.07 ms | 120 |
| 24 题 / v2 旧读出 | 9.63/11.77 s | 28.37/47.92 ms | 120 |
| 新 12 题 / v2 双通道 | 9.66/13.46 s | 31.35/48.62 ms | 60 |

这暴露了 v1 每条经历重建因果历史前缀 FTS 的工程成本。不同 adapter 依次学习、机器上存在并发任务，
不据此宣称算法普遍加速多少倍。五次查询不是独立问题；具体消息对数量与逐次计时见
[adapter-timings.json](../../../eval/results/2026-10-19-scheduler/adapter-timings.json)。

## 固定 36 题重复与基线

使用[既定公开协议](../memory-evaluation.md)：相同完整原文消息对、最多 10 对/24,000 字符、
BGE-small-en-v1.5 384 维/512 截断、相同 reader/judge 与日期。原始 graph 检索正文及向量冻结，
不重新调算法；新 BM25/RRF/近期窗口使用相同原文投影，逐条验证来源身份与正文。
RRF 使用预定 k=60，未根据答案调参。

| 方案 | 正确数 | 正确率 | 标注消息对覆盖率 |
| --- | ---: | ---: | ---: |
| 无记忆 | 3/36 | 8.33% | 0% |
| 近期原文窗口 | 4/36 | 11.11% | 3.13% |
| FTS5 BM25 | 28/36 | 77.78% | 88.54% |
| Dense | 30/36 | 83.33% | 92.71% |
| Dense/BM25 RRF | 30/36 | 83.33% | 95.83% |
| Akasha v1 | 30/36 | 83.33% | 92.71% |
| Akasha2 旧 graph-only 读出 | 26/36 | 72.22% | 80.73% |
| 标注证据 Oracle | 32/36 | 88.89% | 不作检索指标 |

覆盖率仅对存在标注完整消息对、非 abstention 的 32 题计算；回答分母始终保留全部 36 题。
Oracle 使用标注位置，只用于诊断，不是普通检索方案或严格数学上界。
同配置上一轮 Dense/v1/v2 为 28/27/27，本轮为 30/30/26；这是生成与判分波动，不能归因为代码提分。
BM25、RRF 是检索控制，不是 Mem0、Zep 或 Hindsight 的实际实现或成绩。

## 问题保留集

先冻结[24 题 selection](../../../eval/results/2026-10-19-scheduler/holdout-selection.json)，每类四题，
排除先前全部 36 个 question ID，再准备向量、检索与回答。分割单位是问题；原始干扰语料可能重叠，
不是不同用户/不同文档的完全独立泛化实验。保留集未用于调学习参数或切换默认引擎。

| 方案 | 正确数 | 正确率 | 标注消息对覆盖率 |
| --- | ---: | ---: | ---: |
| 无记忆 | 3/24 | 12.50% | 0% |
| 近期原文窗口 | 3/24 | 12.50% | 0% |
| FTS5 BM25 | 18/24 | 75.00% | 76.19% |
| Dense | 17/24 | 70.83% | 78.57% |
| Dense/BM25 RRF | 17/24 | 70.83% | 79.76% |
| Akasha v1 | 17/24 | 70.83% | 76.19% |
| Akasha2 旧 graph-only 读出 | 13/24 | 54.17% | 54.76% |
| 标注证据 Oracle | 24/24 | 100% | 不作检索指标 |

回答各组 24 题、零运行错误；覆盖率仅对 21 道有完整标注对的非 abstention 题求题均值。
旧读出的时间推理为 0/4，促使进一步检查宿主读出；它不能代表补齐通道后当前 adapter 的成绩。

## 修复读出与同图诊断

上游 `plugins/akasha/recalls.py::select_hits` 包含两条通道：精确 dense 最多五项，随后读取图
completion，去重后各通道按时间展示。我们原来直接使用 completion，算法 parity 因而没有覆盖最终证据集合。
现以独立 selector 补齐；时间/strong、抑制节点、原文绑定与 query 不学习保持。

Kirakira 的 limit 仍是总记录数，上游是 completion 预算外加 dense。这一接口差异明确保留，
所有控制仍限十对/24,000 字符。未扩大预算后再宣称提分；见 [ADR0012](../../decisions/0012-akasha2-dual-lane-readout.md)。

恢复相同 24 个图后重新 query，每题查询和关闭前后完整学习状态摘要不变。新读出为 **18/24**，
旧为 **13/24**；21 题标注消息对覆盖率 **54.76% → 66.67%**。
五题回答由错变对，没有反向变化；五题证据覆盖增加，两者不是完全相同的集合。
这 24 题已经观察过，属于诊断重测；回答变化还包含生成与判分波动，不能当作新泛化证据。

## 新 12 题验证

另以固定 seed 冻结 [12 题 selection](../../../eval/results/2026-10-19-scheduler/readout-selection.json)，
每类两题，排除上述全部 60 个 question ID。新旧读出使用相同学习图、cue、原文和预算；
graph-only 只在 eval 中作为消融，没有增加生产兼容分支。

| 方案 | 正确数 | 正确率 | 标注消息对覆盖率 |
| --- | ---: | ---: | ---: |
| 无记忆 | 0/12 | 0% | 0% |
| 近期原文窗口 | 1/12 | 8.33% | 4.17% |
| FTS5 BM25 | 11/12 | 91.67% | 100% |
| Dense | 10/12 | 83.33% | 100% |
| Dense/BM25 RRF | 10/12 | 83.33% | 100% |
| Akasha2 旧 graph-only 消融 | 9/12 | 75.00% | 91.67% |
| Akasha2 当前双通道 | 11/12 | 91.67% | 100% |
| 标注证据 Oracle | 9/12 | 75.00% | 不作检索指标 |

各组零运行错误，这 12 题无 abstention。Oracle 的两道 preference 回答被判 no；一道 multi-session
问题因缺少一项费用而拒绝给出总额。标注证据投影、reader 理解/策略与 judge 都可能影响结果，
Oracle 不是严格数学上界。
结果与 BM25 持平，不能支持图记忆全面优越。此轮未重建 v1，不给 v1 拼接其他题组的分数。

双通道唯一错误是 `54026fce`：reader 给出了远程同事联系与 virtual coffee 的建议，judge 仍判 no。
保留该判分而不人工改成 yes；偏好适配的质量仍需独立评委检查，不能把证据覆盖 100% 写成任务完成 100%。

## Mem0 OSS 同题系统对照

实际运行 `mem0ai==2.2.1` 的 OSS Memory；本地 Qdrant、相同 BGE 权重与 query 前缀，telemetry
关闭，未安装可选 spaCy NLP extra。保留 SDK 原生 additive 提取、hash 去重、存储与搜索，
只将模型传输替换为受校验的 Step Token Plan 客户端。没有拿自写摘要检索冒充 Mem0。

选择来自冻结 24 题中每类预定前两题，共 12 题，包含 9 道非弃答、3 道 abstention。
它与上节的新 12 题不是同一组，分数分别呈现。每题隔离库，每八个完整来源会话一次 add(infer=True)，
保留日期、角色和原文；问题与答案不进提取。来源 ID 有时编码 answer/noise 标签，已从模型可见内容
剔除，并在实际传输前拦截题目/来源 ID 泄漏。

Mem0 输入包含原始不完整消息，图实验只学习完整 user/assistant 对；Mem0 返回最多十条提取事实，
图实验返回最多十对原文，都限 24,000 字符。日期在 Mem0 中还包含来源批次范围。这些是系统/投影
差异，不等价于只更换一个排序器；提取事实的 gold pair recall 记 N/A，不给零分。

最终写入 **12/12 成功**，同一 reader/judge 的 12 组回答均正常结束。

| 方案 | 全部 12 题 | 非弃答 9 题 | abstention 3 题 |
| --- | ---: | ---: | ---: |
| 无记忆 | 3/12 | 0/9 | 3/3 |
| 近期原文窗口 | 3/12 | 0/9 | 3/3 |
| Dense | 7/12 | 4/9 | 3/3 |
| Dense/BM25 RRF | 8/12 | 5/9 | 3/3 |
| Akasha v1 | 8/12 | 5/9 | 3/3 |
| Akasha2 旧 graph-only | 7/12 | 5/9 | 2/3 |
| FTS5 BM25 | 9/12 | 6/9 | 3/3 |
| Akasha2 当前双通道 | 9/12 | 6/9 | 3/3 |
| Mem0 OSS 2.2.1 | 9/12 | 6/9 | 3/3 |

同为 9/12，错误分布不同。两道 assistant 历史题，双通道 2/2、Mem0 0/2；两道 preference，
双通道 1/2、Mem0 2/2；两道 knowledge-update，双通道 1/2、Mem0 2/2。
Mem0 的失败不是两个简单“没见到实体”的例子：

- `6222b6eb` 的摘要保留 SIAC_GEE、6S、Sen2Cor、MAJA 名称，但没保留“SIAC_GEE 使用 6S”的关系。
- `ac031881` 的文字冒险摘要保留 file 54/LIV，但没保留 jumpsuit 上的标识与它的联系。

判分结束后只读查看两个本地 Qdrant 库，分别有 129/159 条事实；相应关键词只找到上述摘要，
与 reader 所说的缺少关系一致。诊断见 [mem0-assistant-diagnostic.json](../../../eval/results/2026-10-19-scheduler/mem0-assistant-diagnostic.json)。
这是这两道题与当前配置的观察，不是所有提取式记忆都会失败的结论。

### 用量与成本

Mem0 共消费 **560 个来源会话**，记录 **74/74 成功 HTTP 响应**，无失败重试；写入响应报告
input **1,905,844**、output **677,774**，合计 **2,583,618 tokens**。
每题初始化+提取+本地 embedding+写入的 p50/p95 为 **611.79/783.82 秒**；
搜索含本地 query embedding，为 **24.27/39.71 ms**。四个 worker 并发，不能把每题中位数当整批墙钟时间。

相同 12 题的 reader 用量另列；不含 judge 和写入：

| 方案 | Reader input tokens | Reader output tokens |
| --- | ---: | ---: |
| Akasha2 当前双通道 | 53,753 | 4,581 |
| FTS5 BM25 | 54,472 | 4,610 |
| Mem0 OSS | 9,564 | 4,398 |

Mem0 的 reader 输入少约 **82.2%**，但额外支付了上述写入模型用量；没有测长期复用后的摊销，
不据此宣称系统总成本更低。图检索的缓存 embedding 计时与 Mem0 的实时 embedding 计时口径不同，
也不计算速度比。完整分类、judge 用量与延迟见
[memory-comparison-summary.json](../../../eval/results/2026-10-19-scheduler/memory-comparison-summary.json)。

两个 QA 前预检完整保留但不计分：最初每会话/8,192 上限有两题出现输出截断，之后统一为八会话/32,768；
另一次预检发现来源 ID 可提示标签，在开始 QA 前中止。最终协议从空库重跑全部固定 12 题，
没有混用中止轮的成功案例。未恢复的写入错误会直接按 ingestion 失败计入回答分母，不让 reader 猜答案。

## 工程验证与结论范围

全量 **694 passed、4 subtests passed**（51.06 秒），22 条既有 Telegram 警告。
读出回归覆盖真实 SessionDB、空 completion 仍能精确召回、去重、总预算、时间/strong 和状态不变。
修复后真实 Step5 记忆工具与重启链 **6/6**；该功能验证使用 hash 向量，不能代替上述语义评测。

36+24+12 共 **72 个去重 question ID**。重复 reader、同图诊断和 Mem0 同题子集均不增加独立问题数，
各轮完整保留，不从不同运行挑最好答案拼分。v1 24 题的分离计时也揭示历史前缀 FTS 逐条重建的
学习开销；本轮没有优化该路径，不把它归因于图算法的普遍成本。

本批质量实验共 **612 组回答/判分**，reader 与 judge 各 612 个成功响应，零运行错误；再加 Mem0
写入的 74 个成功响应，共记录 **1,298 个成功模型响应、5,114,677 个返回 usage tokens**。
这里不包含 Runtime 场景、两个中止预检或不可见失败用量，不当作整个账户的账单。

## 复现和边界

命令与各阶段口径见[评测手册](../memory-evaluation.md)，逐题回答、错误、用量与证据覆盖见
[原始结果](../../../eval/results/2026-10-19-scheduler/README.md)。
同图诊断后 selector 仅在模块 docstring 补 MIT 归属，原摘要未改写；旧摘要可精确重构，执行 AST
不变。新 12 题使用最终源码；差异见 [source-provenance.json](../../../eval/results/2026-10-19-scheduler/source-provenance.json)。
当前实验只有一个英文 embedding 和同模型 judge；小样本指标不是官方全 500 题成绩。
问题分割不保证历史独立；模型别名和在线负载会变化，未估计置信区间。返回 usage 只汇总可见响应，
不是包含所有失败与中止请求的账户账单。保留 v1 与显式 opt-in 的 Akasha2，不自动迁移用户记忆。
