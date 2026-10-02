# 记忆评测

记忆评测分两层：免费、确定性的链路 Gate，以及需要真实模型的质量评测。两者不能混报。

## 确定性 Gate

离线 runner 在隔离 workspace 中对 DefaultMemoryEngine 和 Akasha v1 执行同一批样本，检查：

- 数据能摄入并持久化；
- query 能返回证据；
- source reference 可以回到原样本；
- 不同引擎和 workspace 不串数据；
- EM、F1、MRR 等基础指标计算稳定。

```bash
uv run pytest -q tests/test_memory_eval_offline.py
```

这层适合 CI，能拦住 wiring、持久化和证据链回归，但不能判断模型回答是否聪明。

## 在线链路 Smoke

```bash
uv run kirakira-verify-online
```

该验证使用真实 provider，检查 embedding、Default 工具链、上下文投影和 Akasha 证据消费。最新结果见
[验证记录](./verification.md)。

## 模型质量评测

完整 LongMemEval 还需要真实 AgentLoop 生成回答，并用官方口径或明确的 Judge 计算质量。运行时应把以下
信息分别报告：

- 数据集版本和样本范围；
- engine 与配置；
- retrieval 指标；
- answer Judge/F1/EM；
- token、请求数和费用；
- 失败、跳过和解析异常。

## 当前边界

尚未完成的质量项只在[当前状态](../status/current.md)维护，包括完整 LongMemEval、knowledge update 的
replacement oracle、Akasha 新版 parity 和 PersonaMem schema 漂移适配。离线 Gate 通过不等于这些质量项
已经通过。

相关实现位于 `eval/longmemeval/`，架构见[记忆架构](../architecture/memory.md)。

## v1 事务学习与显式重建

```bash
uv run pytest -q tests/test_memory_commit_cycle.py tests/test_proactive_decisions.py tests/test_drift.py
uv run python -m scripts.replay_akasha --help
uv run python -m scripts.replay_akasha \
  --source /absolute/workspace/sessions.db \
  --output /absolute/workspace/memory/akasha-candidate.db \
  --model exact-stored-embedding-model
```

命令对原始 SQLite 做只读一致性备份，预检合格完整消息对的固定向量，再在同一文件系统暂存并原子发布新库。
输出已存在、缺失/损坏向量、混合维度或非法消息时间都会失败；不请求 provider、不覆盖源库、不自动切换引擎。
输出含 learned/skipped 数量及输入收据 digest；skip 指不属于学习的 assistant 消息，不把缺向量伪装成成功。

默认使用 `AkashaConfig` 默认算法参数。若线上配置有改动，传
`--config-dir /absolute/workspace/plugin-data/akasha-builtin`，该目录必须确实包含 `config.local.toml`；
请按实际 workspace 配置目录填写。比较 online/replay 时必须匹配算法配置、模型身份、来源及强化标记。
确认候选图后，停止 runtime，再修改 Akasha 的 workspace 内 `db_path` 指向候选库，保留原库以便回退。
本轮未对真实用户历史执行重建或切换。

旧 v1 图无消费收据，仅继续摄入新 turn 并不能证明全图 replay parity。
手动删除 sidecar 节点也会改变状态，完整重建会按原始事实重新产生它；删除原始事实与删除索引是不同操作。

## 下一阶段质量实验设计（尚未执行）

| 问题 | 对照 | 指标/控制变量 |
| --- | --- | --- |
| 联想是否带来收益 | dense-only / v1 dense+ripple / v2 | 相同 embedding、query、时间截点、输出预算；Recall@k、MRR、证据准确率 |
| 能否处理长期变化 | 多会话、偏好变化、相似干扰、时间顺序题 | answer EM/F1 或固定 judge；旧事实误用与无证据回答单独计数 |
| 主动是否有价值 | 无记忆 / dense / dense+ripple | 人工标注 share/skip、误打扰率、漏掉的高价值事件、依据引用正确率 |
| 是否依赖轮询抽奖 | 固定同一事件流，变更 tick 频率与重启点 | 决策次数、重复投递、候选遗漏、每有效分享模型成本 |
| 是否适合长期运行 | 不同历史长度、故障注入 | p50/p95 检索/学习时延、内存、恢复后图一致性 |

先固定小规模人工审阅集及来源，再扩展 LongMemEval；不得用相同模板生成题目和历史来证明泛化。
每个样本保留首个有效 attempt、失败和解析异常，不只选最好的一次。算法质量与本轮事务正确性分别报告。
