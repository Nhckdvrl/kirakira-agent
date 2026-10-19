# 原生组合内核与主链重接验证

本轮固定 Akashic `21b38f0e102d9e1bdc5576725635dce29f135c0c`，2026-10-20 完成验证。
Context/Fiber/Effect 与直接依赖逐文件保持原样；包含 Akasha2 的检查共 **33 个固定文件**。
模型、工具、MCP、Ledger、来源、回复程序、交付与控制业务归各自普通 owner。
架构和实际边界见[Runtime 架构](../../architecture/overview.md)。

## 离线验证

统一 `.venv` / Python 3.14.6：最终 `705 passed, 22 warnings, 4 subtests passed`（55.50 秒）。
22 条警告均来自既有 Telegram `retry_after` 弃用提示。

主要合同覆盖：原生依赖激活和关闭顺序、实际调用保留、旧入口拒绝、物理取消、失败清理重试；
连续模型替换后的默认插件及原生工具重接、后台服务重启、Ledger/准入身份保持；
MCP 独立连接和部分 setup 失败后的清理责任；稳定输入回执、暂停续跑、已完成效果复用、
未知效果核实、来源替代、最终提交回滚，以及真实 Socket 管理入口。

构建 wheel/sdist、CLI、内嵌 JS 语法、致命静态错误、功能清单、固定源码与 Yoyo 历史不可变检查通过。
不访问真实 workspace 数据库，不改动已注册迁移或 Akasha2 算法。

## 测试精简

测试文件由 **83 减至 77**，全量用例由 738 收敛到 705；数量不作为覆盖率或完成比例。

- 删除旧全局 generation / snapshot / reload journal 协议的三个测试文件。
- 总线事件并入总线测试，记忆资源关闭并入记忆服务测试，旧 SDK 能力并入插件图测试。
- 历史不可变合同并入 Session 测试，变更影响选择并入架构合同；删除重复 Reference 独立性扫描。
- 删除纯构造器/常量/目录存在断言、已删除容器的内部断言、对无关空 registry 的无效 MCP 断言。
- 自建图测试替换为原生内核合同，保留真实插件/入口、失败恢复、效果和持久事实的回归。

开发时先用 `scripts/check_change_impact.py --run` 选择相关合同；最终提交仍跑全量测试。
固定源码检查单独运行，避免再增加重复目录断言文件。

## 真实 Step Token Plan

所有请求只使用 `https://api.stepfun.com/step_plan/v1`，模型 `step-5-preview`，
`reasoning_effort=medium`，原生流式路径；不使用现金账户或在线 embedding。
每个场景有独立临时 workspace。

| 批次 | 场景通过 | 模型请求尝试 / 成功响应 | 返回 usage tokens | 记录 |
| --- | ---: | ---: | ---: | --- |
| 接入批次 | 14/14 | 44 / 40 | 107,303 | [step-r1.json](../artifacts/native-composition/step-r1.json) |
| 功能复测 | 16/16 | 48 / 44 | 113,356 | [step-r2.json](../artifacts/native-composition/step-r2.json) |
| 最终代码 | 8/8 | 24 / 22 | 56,741 | [step-final.json](../artifacts/native-composition/step-final.json) |

合计 **38/38 场景**、116 次请求尝试、106 次成功响应、277,400 返回 tokens。
取消流和注入的临时错误使尝试数大于完整响应数，不按失败任务计数，也不补造缺失 usage。
前两批为接入期间记录；只有最后一批的逐文件哈希与本次最终实现全部一致。

最终八类场景：显式独立读取与写入屏障、已执行效果后的生成重试、deferred tool search、
工具错误纠正、文件读写重启、流取消排空、压缩重启，以及后台服务运行中的模型 provider 替换。
后者断言新 Conversation 实例接回、共享 Ledger/准入对象保持身份、四条原文保留且模型继续回复。

验证器：`eval/scenarios/core.py`；结果保留实际工具调用、断言、请求信息和源码摘要。
这些是合成功能合同，不是外部任务基准或性能提升证据；没有重新开展记忆质量评测。

## 范围

原生内核固定不等于全量功能完成。完整清单继续登记 70 个生产插件及 3 个跨目录面，
其中 54 项部分接入、16 项未接入；不把这些项目标成整项完成。
原生 Task 服务的业务接线、完整执行宿主、后台结果注入和其余产品功能继续按清单推进。
