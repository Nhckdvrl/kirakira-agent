# 2026-10-19 评测文件

协议与解释见 [实验报告](../../../docs/operations/evaluations/2026-10-19-memory.md)。
`native-*` 是显式 Step5 reasoning_effort=medium、8192 输出上限的主结果；`round1-*` 是初始参数的保留记录。
两轮各有 36×4 个普通结果和 36 个标签辅助 oracle，共 360 组；不能把它们当成 360 道独立题。

`retrieval-compact.jsonl` 不含完整检索正文；保留引擎 ID、来源 session、预算、trace 和事后覆盖诊断。
其中 `gold_pair_index` 仅在生成后由官方标注匹配，未传给普通 reader。非空索引去重数除以 gold_pair_count
可复算覆盖率。32 题有可用标注完整消息对，3 道弃答及 1 道投影缺口不进入该指标；36 题均参与回答评分。

`case-matrix.csv` 可直接比较每道题。正确数也可从逐题 JSONL 复算：

```python
import json
from collections import defaultdict
from pathlib import Path
counts = defaultdict(lambda: [0, 0])
for line in Path('native-results.jsonl').read_text().splitlines():
    row = json.loads(line)
    counts[row['arm']][0] += int(row['correct'])
    counts[row['arm']][1] += 1
print(dict(counts))
```

`manifest.json` 记录来源、软件、用量边界和文件 SHA-256；原始数据/向量/数据库在仓库外。
`core-*`、`drift-*`、`wake-*` 使用公开构造的测试数据与本地回执，不含真实用户消息或密钥。
