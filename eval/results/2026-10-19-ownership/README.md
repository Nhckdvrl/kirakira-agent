# 提供者归属与工具链验证

本目录保存 2026-10-19 本轮组件收敛后的实际结果；[报告](../../../docs/operations/evaluations/2026-10-19-ownership.md)解释协议和边界。

| 文件 | 内容 |
| --- | --- |
| core.json | Step5 12 个 Core/Wake 决策场景 |
| drift.json | Step5 4 个 Drift 场景 |
| wake.json | Step5 1 个 fetch/决策/投递/反馈/去重整链 |
| memory.json | Step5 6 个记忆工具场景；本地 hash 向量，仅作链路验证 |
| tests-py314.txt / tests-py311.txt | 两种 Python 全量回归，各 662 项与 4 subtests |
| incremental.txt | 最后的插件 API 与影响选择器增量检查 |
| manifest.json | 上游固定点、源码和制品 SHA256、统计口径 |

复跑脚本为 eval/scenarios/decisions.py、verify_drift_step_plan.py、verify_wake_step_plan.py 和 verify_memory_step_plan.py。
密钥、测试数据库与工作目录不随本目录提交；源码摘要固定本轮实现，不用当前 main 的浮动内容解释旧结果。
