# 2026-10-19 事件记忆与恢复

全为本项目固定生成的合成输入或此前公开语料测量，不含用户真实历史、密钥、权重或数据库。
API 只走 Step5 Token Plan。代码、协议与结果分别固定，不能把合成 24 题混入公开 72 题总分。

- `fixture.json`、`selection.json`、`protocol.json`：24 题、六类各四题，模型调用前固定。
- `embedding-manifest.json`：实际免费 BGE 权重/环境与输入摘要；向量和权重留在仓库外。
- `retrieval-original*`、`retrieval-final*`：缓存升级前后两套索引，三个 arm 的 reader 输入逐字节一致。
- `retrieval-baselines*`、`coverage.json`：BM25、RRF、recency，按真实原文身份计算覆盖。
- `mem0-ingestion*`、`retrieval-mem0.jsonl`：原生 Mem0 OSS，24/24 写入、48 次成功模型响应。
- `answers-baselines*`、`answers-graph*`：分别四组与三组，合计 168 个不同 case/arm；
  `*.run.json` 在调用前固定身份，`*.summary.json` 是原始汇总，没有合并选择最好答案。
- `evidence-initial.json`：早期 234 条引用展开与重启检查；`recovery-final.json` 使用最终源码，
  比较全部学习表与完整消费 JSON，仅凭源库从空图重建，并验证网络连接尝试为 0。
- `source-benchmark-initial.json`、`source-benchmark-final.json`：同三张图的源连接数与每次计时；
  最终版本明确保留与旧版本的浮点末位差，不声称逐位分数与旧 bug 一致。
- `agent-tools-initial.json`、`agent-tools-r2.json`、`agent-tools-r3.json`：三轮严格结果 5/6、5/6、6/6。
  首轮失败缺详细过程；后两轮只增加诊断，断言/流程未放宽。
- `initial-recovery-failures.json`：初始失败日志的完整文本与原字节 SHA，可无损复原；
  包含测试探针初始化错误及实际重复归一化问题。`recovery-targeted-tests.txt` 为修复后 16 项通过。
- `pytest-final.txt`：702 项全量、4 个 subtests，Python 3.14.6。
- `summary.json`：各类正确数、错误和 usage；`verification-manifest.json` 记录最终源码、环境与制品身份。

Mem0 唯一 reader 错误是正确偏好被放进标量字段，严格协议要求数组，主分数保留失败。
专项题较简单，多个基线满分；不支持图记忆普遍领先。完整范围见
[实验报告](../../../docs/operations/evaluations/2026-10-19-episodic.md)。
