# 当前原生 Runtime 对齐验证

参考 Akashic `d8761bb9000a7be8701c65e049b0978879cc6889`，2026-10-20。
源码范围、启用差距和后续顺序见[差距文档](../../status/akashic-gap.md)。
本记录的数字只对应当前生产主链；迁移记录中的旧数字保持原始基线。

## 方法与边界

所有远端业务模型固定 Step Token Plan / step-5-preview。embedding 使用本地
BAAI/bge-small-zh-v1.5 / 512 维 ONNX；权重及 tokenizer 摘要由显式 setup 固定。
同一个 Python 3.14.6 `.venv` 运行生产代码、SDK、评测与本地编码。

受控记忆比较使用 6 个项目、每项目两种易混淆事实，共 12 段真实历史对话；
查询只有项目与所问字段，答案是随机 nonce 值。查询会话由正式会话准入设置为 excluded，
不参与学习。Dense 与 Akasha 使用同一个向量空间、相同 top-3 资料预算、同格式原始消息、
同一个独立 Step5 reader；无记忆 reader 不提供资料。评分对象是独立 reader，
不使用 Agent 已看过自动召回上下文后的最终回答冒充检索质量。
Akasha 的额外 pattern-completion 命中在比较中不超过 top-3 预算。

这是受控功能对比，不能证明 Akasha 优于 Dense 或 Mem0，也不是 LongMemEval / LoCoMo 成绩。
主动内容的生产者高分是显式夹具输入，检验初筛、调查、分享与实际送达链路，
不当作真实兴趣预测准确率。Alert 去重核对 mail_id、accepted 和新消息数量，
不要求第一次与重复接纳的响应字典相同。

原生 model_calls 账本计量 chat，未计量普通 embedding 调用，因此不使用其中的零值
推断没有发生本地编码。重启复用通过 sessions-derived.db 中历史向量摘要及创建/更新时间核对。

上游合成场景独立验证发布、崩溃重放、重建、只读检索与派生数据恢复；
其中固定/随机向量不是语义模型，结果与真实 Step5 / ONNX 场景分别报告。

## 复现

```sh
uv sync --frozen --group dev --group eval --extra providers
uv run --group eval python -m scripts.compare_upstream --reference /path/to/fixed-akashic-checkout
uv run --group eval pytest -q
uv run --group eval python -m eval.native_alignment --workspace /tmp/fresh-eval \
  --key-file /private/path/step-plan.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --output /tmp/alignment.json --repeats 2
uv run --group eval python -m eval.native_proactive --workspace /tmp/fresh-proactive-eval \
  --key-file /private/path/step-plan.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --output /tmp/proactive.json
uv run --group eval python -m eval.upstream.akasha_publish_replay_scenario --source "$PWD" --turns 300
uv run --group eval python -m eval.upstream.akasha_cost_scenario --source "$PWD" --output /tmp/fresh-akasha-cost --turns 272
uv run --group eval python -m eval.upstream.akasha_derived_recovery_scenario
npm run build
npm run typecheck
```

修正 reader 调用后可对同一隔离工作区加 `--reuse-training`，只重跑查询与 reader，
创建新的 excluded 查询会话并保留原历史；该结果的模型账本数字是工作区累计值。
完整失败报告与修正后的报告分别保存，不能将不同运行的重复累计账本相加。
原始服务器日志包含临时登录凭据，仅在私有临时目录中留存。

## reader 预算检查

初始 128-token reader 出现 finish_reason=length，部分只有推理或不完整口令；
在目标原文已经命中的情况下也会被判错。该轮作为低预算测量保留。
最终三组统一使用 1024-token 上限，并保存 reader 原答、结束原因与截断数量。
两轮资料、检索上限和评分方法相同；不把低预算误差表述为图记忆退化。
新增参数 `--reader-max-output-tokens 128` 可复现低预算设置。

## 受控记忆结果

6 个独立问题各运行两轮，每组共 12 次 reader 判断。三组统一使用 1024-token 上限。

| 系统 | 目标原文 Hit@3 | 答案 Exact Match | 输出截断 |
| --- | --- | --- | --- |
| 无记忆 | 不适用 | 0/12 | 0 |
| 同空间 Dense | 12/12 | 12/12 | 0 |
| 原生 Akasha | 12/12 | 12/12 | 0 |

低预算 128-token 轮次中，Dense 为 11/12，Akasha 为 8/12，二者目标原文均命中 12/12。
这组小样本能够证明学习、跨会话检索和 reader 链路成立，不能区分两种检索架构的质量上限，
也没有验证相对 Mem0 的优势。更大规模、关联推理、冲突更新与时间推理仍需单独的公开资料评测。

[最终 1024-token 结果](../artifacts/2026-10-20-alignment/memory.json)、
[128-token 原始结果](../artifacts/2026-10-20-alignment/memory-128.json)。

## 主动场景的证据要求

首个高分内容夹具只有“已确认”自述，没有可核实来源；后续轮次被 Step5 以注入/占位内容为由跳过。
该结果保留，不把高分解释为应当强制发送。最终夹具由用户明确指定本机只读诊断 URL，
实际启动 HTTP 服务提供观测、原因与修复记录，再提交带来源链接的 Content。
验收同时检查真实 web_fetch 请求、结构化 screen/share 决定、Delivery 回执、保留来源链接与生产者 ack。
这验证资料核实与送达，不代表真实外部设备或诊断准确率。

主动夹具曾在 4096-token 上限耗尽推理预算而没有完成 remember 调用；最终明确单步任务，
为整组场景统一设置 8192-token 上限，保留之前的长度失败与后续未完成结果。
生产发行默认参数没有因此被修改，原生主链在长度限制时不执行未完成的工具调用。

工作台检查携带本机门禁 Bearer，确认返回真实 Dashboard 与 bootstrap，而非登录页的 HTTP 200；
检查运行时返回 HTML 的 import map 位于模块脚本和 modulepreload 之前。
源码已更新但插件仍提供旧资产的问题，由已登记的发行 staging 构建补丁修复。

## 功能与实际工作区结果

| 验证组 | 最终结果 |
| --- | --- |
| Step5 原生主链：文件、Shell、MCP、托管进程、子代理、更新、控制、附件、计划任务、重启 | 13/13 |
| 源码更新相关功能：图学习、诊断、告警送达/去重、低兴趣跳过、背景事件、安装插件、压缩边界、恢复 | 8/8 |
| 主动与记忆反馈：空图 reindex、SDK 校验、remember、核实后分享、ack、forget | 6/6 |
| 认证工作台、bootstrap、项目目录与 import map 顺序 | 1/1 |
| 真实工作区 | 118 条本地向量补算；总缓存 122 条；原生图 61 个闭段 |
| 历史事实保护 | 本轮之前 126 条消息的 ID、顺序、时间、作者、来源、正文与元数据逐条一致 |
| 私有备份 | 74 份持久文件，全部摘要与 SQLite 完整性检查通过 |
| 原生发布 / 重放 / 完整重建 | 300 段，三条路径逻辑摘要一致 |
| 原生增量 / 只读查询 / 发布故障 | 272 段，全表一致、查询不改图，3 个故障边界通过 |
| 派生丢失恢复 | 3 段，补回 6 个固定向量，原消息和图不变 |

主链探针与宿主都使用 Python 3.14.6。发行入口在原生宿主冻结环境前选择当前虚拟环境，
没有放宽原生制品路径校验或允许业务调用覆盖宿主 PATH。

[主链](../artifacts/2026-10-20-alignment/core.json)、
[更新功能](../artifacts/2026-10-20-alignment/functional.json)、
[主动与反馈](../artifacts/2026-10-20-alignment/proactive.json)、
[工作台](../artifacts/2026-10-20-alignment/workbench.json)、
[实际工作区](../artifacts/2026-10-20-alignment/workspace-upgrade.json)。

隔离工作区共记录 455 次 chat 账本调用：453 次 success，2 次因关闭过程取消而记为 error
（failure=CancelledError）。全部远端模型连接为 Step Token Plan；输入 2,745,935 token，
输出 56,663 token。重复使用的记忆工作区仅按最终累计账本计一次；本地编码和额外设置探测不在此计数内。
账本 success 表示调用返回，不等同任务完成或答案正确。包内验证与真实工作区的额外调用单独记录。
[调用范围及原始计数](../artifacts/2026-10-20-alignment/model-usage.json)。

首次运行中发现的 OwnerCall、重复 Alert 收据、reader 显式模型/预算、节点 0、
宿主解释器、制品路径与环境权限、未核实内容、资产 staging、wheel 资源和既有 selection 问题均保留记录。
原始失败与修正后结果分别保存；最终实现沿用原生权限、业务决定与兴趣阈值。

完整回归曾在两项真实后台 Shell 的首轮等待上超时：消息时间证明工具结果在约 5.15 秒到达，
原等待为 5 秒；聚焦重跑两项均通过。后台生命周期夹具现显式使用 bash / login=false，
避免将用户登录 profile 的冷启动纳入该场景；等待期限、后台 yield 参数和生命周期断言保持原值。

完整回归最终 **93/93**。wheel / sdist 的 930 个固定文件逐字匹配，发行入口与
本地扩展逐字匹配；从隔离 wheel 路径启动正式 Runtime、导入 SDK 并完成真实 Step5 回复。
包内真实密钥匹配为 0、运行数据库为 0。前端完整构建与 TypeScript 检查通过。
[包内验证](../artifacts/2026-10-20-alignment/package-checks.json)、
[单元回归](../artifacts/2026-10-20-alignment/unit.log)。
