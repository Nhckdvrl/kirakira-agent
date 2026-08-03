# Kirakira 与 Reference 能力审计

审计基线：`Reference` commit `af49848937c4b62abb2f40a7d91b5f90ea71be6d`
(2026-08-03)。本文只比较**实际调用链与可验证合同**，不以文件名数量或代码行数判定完成。

## 结论

后端 Runtime 的承重架构已经对齐到相同的功能 owner：agent、bootstrap、bus、core、infra、
session、memory、plugin、proactive、frontend 与 eval 各自只有一个正式实现位置。Kirakira 保留现有
功能并把实现迁入这些 owner，没有生产代码依赖 `Reference/`，也没有为了兼容保留第二套
`kirakira_agent/*` 实现。

“架构对齐”不等于逐文件复制：Reference 的 `schema/`、`types/`、`prompts/` 等细分根在 Kirakira
由 `core/schema.py`、各 owner 类型与 `agent/prompting/` 承担；benchmark/docker/systemd/mobile
产品工程不属于当前后端 Runtime 对齐范围。边界由 `tests/test_architecture_boundaries.py` 固化。

## 已闭合的主要 gap

| 能力面 | Kirakira 当前合同 |
| --- | --- |
| 启动与 workspace | supervisor/gateway 分层、单实例锁、readiness、Yoyo 统一迁移账本、migration append-only 检查 |
| 上下文治理 | 持久历史 append-only；每次请求重渲染投影；具名 section 降载；工具批次压缩；活跃 execution pin；exact/partial/unavailable usage |
| 模型 Runtime | async/non-streaming/streaming；DeepSeek reasoning/tool call；context/safety/retryable 错误分类；轻模型安全回退 |
| 工具 | ToolMeta 风险/发现/来源；deferred search；统一 Shell/PTY/stdin/输出/进程组；读写与 SSRF 边界 |
| 子 Agent | inline/background、隔离 session/owner、全局容量、取消、完成回注与资源清理 |
| Scheduler | at/after/every、5/6 段 cron、IANA timezone、instant/soft、隔离 soft session、容量边界 |
| Plugin/MCP | 程序化能力声明、代际、snapshot lease、热重载、失败保留旧代际、安装后激活、MCP watcher/admin |
| 记忆 | MemoryServices/engine/plugin 边界、Default 结构化记忆、Akasha v1、Markdown maintenance、证据与检索回放 |
| Proactive/Drift | Gate、module DAG、三通道、冷却、delivery dedup/ACK、源 feedback、tick/step 轨迹、Drift continuity |
| 控制与观察 | programmatic turn、事件流、中断、tool chain、context trace、主动 tick-step 数据 |
| 质量门禁 | 584 个离线测试 + 4 subtests；语义合同；change-impact 选择器；隔离 workspace 在线验证器 |

## 当前有意保留的差异

### 1. Akasha v1，不升级 v2

当前 `plugins/akasha/` 已可真实摄入完整 turn、建立图/RAR 召回、持久化证据、强化并把召回注入
模型；在线验证通过。Reference 最新 Akasha v2 更厚的重建、恢复、事务与运行维护合同尚未迁移。
这是当前明确批准的延期，不影响 v1 可用性，插件/engine/admin 边界已经为之后替换留出空间。

### 2. 前端与移动后端

Kirakira 保留现有 TUI、本地 Web 与 dashboard；没有复刻 Reference 的共享 React/Android pairing、
mobile realtime、长正文 Range 恢复、WebUI generation/OTA。这一产品面已明确暂不处理，不能据此
宣称移动产品完全一致。

### 3. Reference 的运维与评测厚度

Kirakira 已有最小 semantic Gate、LongMemEval 兼容 eval 和真实在线 smoke test，但没有完整复刻
Reference 的 Docker/Harbor campaign、mutant、私有 scenario oracle、systemd 与多环境发布体系。
这些是复杂度与运维深度差异，不是当前 Runtime 功能缺失。

## 仍可继续加厚、但不阻塞当前可用性的项目

| 项 | 当前已有 | 之后可扩展 |
| --- | --- | --- |
| 主动治理 | 冷却、去重、feedback、tick/step trace | 日配额、累计 hazard、兴趣向量、更多数据源策略 |
| Provider profile | OpenAI-compatible + DeepSeek tool/usage/error 合同 | 多 provider 目录、reasoning effort/settings UI |
| 进程守护 | macOS/本地 supervisor、boot commit/readiness | Reference Linux guardian/systemd 的全部竞态合同 |
| 插件分发 | 本地包安装、热激活、回滚与代际 | 非 git 源、版本缓存、独立 MCP venv 准备 |
| 轨迹展示 | turn/tool/context/proactive step 数据齐全 | 新的可视化前端页面（当前前端范围外） |

## Reference 独立性合同

生产源码不得 import `Reference`，不得用 `Path("Reference")` 读取运行数据，也不得把上游 checkout
加入 wheel。以下命令必须在 `Reference/` 不存在时仍成功：

```bash
uv run pytest -q
uv build
python -m kirakira_agent --help
```

上游 commit pin 只允许作为纯文本来源元数据存在。新能力应优先移植 Reference 的可复用实现，但
移植完成后必须由 Kirakira owner 自己承重。
