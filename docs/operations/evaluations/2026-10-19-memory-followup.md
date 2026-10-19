# 记忆：公开机制复测、任务质量与检索对照

日期：2026-10-19。所有生成使用 Step5 Token Plan、medium、8,192 输出上限；免费 BGE embedding
与项目共用 `.venv` / Python 3.14.6，可选 eval 依赖由同一 uv.lock 固定。用户真实会话没有参与实验。

## 参照与范围

Akashic `d85b00c` 已退役 LongMemEval/PersonaMem CLI。当前公开材料重点是
[机制与成本](https://github.com/kachofugetsu09/akashic-agent/blob/d85b00c233686b68b082052b1b20123905aeab13/docs/design/akasha-memory-cost.md)
与[公开合成场景](https://github.com/kachofugetsu09/akashic-agent/blob/d85b00c233686b68b082052b1b20123905aeab13/scripts/akasha_cost_scenario.py)。
独立引擎另有[数学性质与重放验证](https://github.com/kachofugetsu09/akasha-v2-engine/blob/e85dc2c66ff4eb16af118c638d72e7c32f3dc69a/docs/validation.md)。
上游私人经历和 V8 capture 没有公开，不能伪称复测了这些语料或继承其成绩。

本轮分别验证三件事：相同输入的完整状态与恢复；查询/学习/发布成本；公开问题上的回答质量。
前两者使用合成输入，第三者使用公开 LongMemEval，二者不互相替代。

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
| Akasha2 | 26/36 | 72.22% | 80.73% |
| 标注证据 Oracle | 32/36 | 88.89% | 不作检索指标 |

覆盖率仅对存在标注完整消息对、非 abstention 的 32 题计算；回答分母始终保留全部 36 题。
Oracle 使用标注位置，只用于诊断，不是普通检索方案或严格数学上界。
同配置上一轮 Dense/v1/v2 为 28/27/27，本轮为 30/30/26；这是生成与判分波动，不能归因为代码提分。
BM25、RRF 是检索控制，不是 Mem0、Zep 或 Hindsight 的实际实现或成绩。

## 问题保留集

先冻结[24 题 selection](../../../eval/results/2026-10-19-scheduler/holdout-selection.json)，每类四题，
排除先前全部 36 个 question ID，再准备向量、检索与回答。分割单位是问题；原始干扰语料可能重叠，
不是不同用户/不同文档的完全独立泛化实验。保留集不用于修改生产参数或切换默认引擎。

结果单独保留，不能与旧题重复次数混成更多独立题，也不从不同运行挑最好答案拼分。

## 复现和边界

命令与各阶段口径见[评测手册](../memory-evaluation.md)，逐题回答、错误、用量与证据覆盖见
[原始结果](../../../eval/results/2026-10-19-scheduler/README.md)。
当前实验只有一个英文 embedding 和同模型 judge；小样本指标不是官方全 500 题成绩。
保留 v1 与显式 opt-in 的 Akasha2，不因算法更新或单次得分就宣称总体领先。
