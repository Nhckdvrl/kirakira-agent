# 验证记录

本文只记录已经实际执行过的验证。功能现状见[当前状态](../status/current.md)，不要从测试数量反推能力范围。

## 2026-10-20：后台结果与最新 main（最新）

核对上游 main `6016bef`：原生组合内核及直接依赖的 23 个文件逐字节一致，整体业务架构仍有差距。
新增独立 Jobs owner，结果按模型步领取，闲时原会话唤醒，消费与回复同事务；已接纳唤醒和领取后
取消都验证了重启恢复。全量 **712 项与 4 项子测试**通过，最终 Step5 Token Plan **64/64**，
139 次成功模型返回；千轮、64 会话、5,000 工具调用验证通过。
两次模型异常输出及旧验证器失败没有删去，全部口径、延迟和边界见
[后台结果与实测报告](evaluations/2026-10-20-background-results.md)。

## 2026-10-20：重构后的性能复核

705 项由 662 个测试节点和 43 个参数化扩展组成。保守清理不等于测试体系已经充分精简。
同机同解释器，对比重构前 `980fcbd` 与当前生产代码 `bdf0aaf`：每版本两次千轮全部通过，
每对 2,077 次模型请求摘要逐项相等。完整主链吞吐中位数 **87.19→54.15 轮/s，下降 37.90%**；
不能宣称性能完全保持。局部流、日志及工具并发基本保持，SQL trace 显示新增程序耐久写入。
本轮没有修改生产逻辑；详见[对照结果与范围](evaluations/2026-10-20-runtime-parity.md)。

## 2026-10-20：原生组合内核与主链

原样复用固定提交的 Context/Fiber/Effect 内核；普通业务 owner 在同一 Root 接回。
统一 Python 3.14.6 全量 **705 passed、4 subtests**，22 条既有 Telegram 警告。
测试文件 83→77；删除旧协议与纯实现断言，合并重复测试，保留恢复、实际效果与真实入口合同。
Step5 Token Plan 三批 **38/38** 场景通过；最终批包含后台服务中的模型替换，源码摘要全部匹配。
构建、CLI、JS、静态检查、33 个固定文件和迁移不可变检查通过。
实际证据及范围见[专项记录](evaluations/2026-10-20-native-composition.md)。

## 历史证据

- [截至 2026-10-19 的验证记录](../archive/2026-10/verification-through-2026-10-19.md)
- [历史专项报告](./evaluations/)与 `eval/results/` 保留原始结果及来源摘要。

## 常用命令

```bash
uv run pytest -q
uv build
uv run python main.py --help
uv run kirakira-verify-online
```

在线验证会读取本地 `config.toml` 中的 provider 和 embedding 配置，但不会打印 API key。验证器使用临时
workspace，不污染真实 Session、Memory 或 Proactive 数据。

## 结果口径

- **已测试**：合同、边界和回归由自动化测试覆盖；
- **已在线验证**：真实 provider 和真实 AgentLoop 已执行；
- **未验证**：没有把 mock 或静态检查包装成实弹结果。

## 尚未覆盖的实弹范围

- Telegram、QQ、QQBot 等外部 Channel 的真实账号端到端投递；
- 完整 LongMemEval 的模型 Judge/F1/EM 质量评测；
- Akasha 新版本的全量图行为 parity；
- 前端、移动端和轨迹可视化页面。

这些边界仍属于对齐范围；不能宣称整体架构及在线端到端验收已经全部完成。
