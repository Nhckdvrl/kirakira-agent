# 0006：共享推理与提交后记忆学习

日期：2026-10-02。状态：accepted。补消费与主动输入版本合同由 [0007](0007-durable-consumption-and-proactive-supersession.md) 扩展。

## 背景

主动 JSON 判别、同步 Drift Agent 和异步被动 reasoner 分别演进，工具策略与终态容易漂移。
Akasha 查询侧的激活可能影响学习；按消息文本反查身份和在线/重放分开的状态路径，使重试难以审计。

## 决策

1. `DefaultReasoner.run` 是三种运行方式共享的模型/工具循环。`run_scoped` 只组装隔离后台调用，
   不写伪造用户消息、不触发记忆学习。输入持久化和投递由调用方 owner 管理。
2. `ReasoningPolicy` 控制工具集合、动态收窄、终结工具、预算末步选择和预算耗尽行为。
   执行器在实际执行前再次授权；排他终结动作与其他调用同批出现时，整批拒绝。
3. Content 只有合法 `share_content` 可以产生投递意图；有效 skip 持久记录，错误按持久 retry_at 重试。
   Alert 保留原文兜底；Drift 草稿必须由 `finish_drift` 收口后才投递。
4. 保留 Akasha v1 检索数学。查询不改变学习图，完整持久 user/assistant 消息对才触发学习。
   `TurnCommitted` 携带稳定消息 ID；旧事件内容匹配只能有唯一结果，不能选“最后一条”。
5. 共享 `apply_committed_turn` 在一个 SQLite 事务里更新图、激活状态和消费收据。
   重复输入幂等，身份相同但输入不同失败；固定 embedding 空间与算法配置，改变须显式重建。
   上一轮共激活状态按 session 存收据，重启后继续使用。
6. 学习只看已发布历史前缀，包括 dense、FTS 和 IDF；全库未来消息不能改变历史回合学习结果。
   离线重建先验证来源和固定向量，再生成全新 sidecar，以原子 no-replace 发布，不覆盖/激活旧库。

## 取舍

- 不复制最新 Akashic 的整个插件组合内核。本轮后台执行不等价于 durable message program。
- 不给任意外部副作用承诺 exactly-once。投递继续采用 ADR0004 的去重窗口语义。
- 第一轮仅有提交事务；第二轮已由 ADR0007 加入 cutover 后的持久补消费。升级前缺口仍需显式重建。
- v1 兼容桥为每次学习重建历史前缀的内存 FTS，成本随历史增长。后续必须测量再选择增量索引或 v2。
- 旧图没有消费收据；允许继续学习新 turn，但对旧 turn 重试不伪造收据，要求显式重建。
  旧图来源及模型身份的完整证明只能由重建取得。
- 离线重建对缺失固定向量严格失败，不调用在线模型填洞。该策略比当前上游显式 skip 更保守，属于本地选择。

## 验证

`test_proactive_decisions.py` 覆盖非法输出/引用/批次、静态和动态授权、冷却期间候选与失败重试。
`test_drift.py` 覆盖共享执行与未收尾草稿；`test_memory_commit_cycle.py` 覆盖不学习查询、稳定身份、
幂等、回滚、重启、重放、新库预检、配置变化和未来全文命中隔离。实际结果见[验证记录](../operations/verification.md)。
