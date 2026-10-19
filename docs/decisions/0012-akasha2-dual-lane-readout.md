# 0012：Akasha2 的精确与联想读出

日期：2026-10-19。状态：已实现；默认引擎与用户历史没有自动切换。

## 问题与决定

原 Kirakira adapter 直接返回 MemoryCycle 的 completion，遗漏了 Akashic 宿主的精确向量通道。
即使学习图与上游一致，模型收到的证据集合也可能不同。算法一致性不能替代读出验收。

`plugins/akasha2/selection.py` 按固定
[上游 select_hits](https://github.com/kachofugetsu09/akashic-agent/blob/d85b00c233686b68b082052b1b20123905aeab13/plugins/akasha/recalls.py)
分离职责：

1. 对 query 与每个 turn 的 user/assistant dense 求相似度，最多保留五个精确命中。
2. 图 completion 沿原始顺序选择，排除已选精确节点及被抑制节点。
3. 两条通道遵循时间与 strong 过滤，各自按原始时间与稳定身份排序。
4. 结果逐条从原文绑定回源，不把向量或摘要当作新事实。

Kirakira 维持通用 MemoryQuery.limit 的总记录预算；completion 填充精确通道之后的剩余位置。
上游的 limit 是 completion 预算、额外保留精确通道。本项目保留这个明确的接口差异，所有 QA 控制
仍采用最多 10 对/24,000 字符，避免扩大上下文后宣称检索变好。

学习、反馈、扩散、遗忘与 SQLite 格式均不改。原图直接恢复，query 不学习，也不推进消费位置。
未给生产引擎增加 graph-only 兼容模式；退役读出只作为 eval 中的诊断消融。

## 验证

真实 SessionDB/engine 测试强制图 completion 为空，精确来源仍保留；同时覆盖双通道去重、
总预算、时间/strong 过滤与读取前后的学习状态。真实 Step5 记忆工具与重启链再次验证。

在同一批冻结 24 题与原图上，旧 graph-only 为 13/24，新读出为 18/24；标注消息对覆盖率由
54.76% 到 66.67%（21 道非 abstention）。这批题已被观察，属于问题诊断，不能当作新的泛化集。
五道题的覆盖增加，回答变化还包含生成/判分波动。

另冻结未参与先前 60 题的 12 道问题，统一预算对比：新读出 11/12、旧 graph-only 消融 9/12、
Dense 10/12、BM25 11/12；图和召回票据不因消融查询改变。规模很小，不宣称全面优越。

完整结果、上下文协议与限制见[实验报告](../operations/evaluations/2026-10-19-memory-followup.md)。
