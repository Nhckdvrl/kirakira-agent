# Kirakira Agent

Kirakira 是一个本地优先、多渠道的 AI Agent Runtime。当前源码的组织边界已经按最新版
Akashic Reference 收敛，但实现仍由 Kirakira 自己持有：`Reference/` 只用于开发审计，生产
import、启动、迁移、构建与测试都不读取它。把整个 `Reference/` 文件夹移走，项目仍应完整工作。

## 三条运行链

```text
用户消息 ─→ 被动 Turn ─→ 上下文投影 ─→ ReAct/工具 ─→ 提交 Session ─→ 渠道回复

定时唤醒 ─→ Proactive Gate ─→ alert/content/context ─→ 判断 ─→ 投递或跳过
                                                   └─ 无推送时进入 Drift
```

- **被动回复**：同 session 串行、跨 session 并行；支持流式模型、工具、MCP、插件、长期
  记忆、子 Agent、调度与中断。
- **主动推送**：电量节流、三类数据源、冷却/去重、真实渠道 ACK、源反馈，以及每个 tick 和
  module step 的持久运行轨迹。
- **Drift**：主动轮次无内容可发时，读取用户的 `drift/skills/*/SKILL.md` 执行可持续后台任务。

## 快速开始

要求 Python 3.11+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv run python main.py setup    # 交互式初始化
uv run python main.py init     # 非交互初始化
uv run python main.py          # supervisor -> gateway
uv run python main.py gateway  # 不经 supervisor 的调试入口
```

没有 `config.toml` 时，默认入口会自动进入初始化向导。旧公开入口继续兼容：

```bash
python -m kirakira_agent --tui
python -m kirakira_agent --plain
python -m kirakira_agent --session research
```

DeepSeek 示例：

```toml
[llm.main]
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com/v1"
api_key = "${DEEPSEEK_API_KEY}"
context_window = 128000 # 按供应商公布值填写

[agent]
max_tokens = 8192
max_iterations = 40

[agent.context]
effective_context_percent = 0.9

[memory]
enabled = true
plugin = "akasha" # 可选 default / akasha；当前可继续使用 Akasha v1

[memory.embedding]
model = "text-embedding-v3"
base_url = "${EMBEDDING_BASE_URL}"
api_key = "${EMBEDDING_API_KEY}"
```

聊天端点通常不提供 embedding，因此记忆向量端点单独配置。

## 当前已经承重的能力

### 上下文治理

- `sessions.db/messages` 是权威历史；正常保存只能追加，不能删除或覆盖旧消息。
- 每次模型请求只渲染一份当前投影：先丢可选 prompt block，再逐级缩小历史窗口。
- 上下文超长重试不会修改数据库；下一轮仍从完整持久历史重新投影。
- 工具 schema、图片和输出预留进入同一预算；压缩不能丢掉仍在运行的 Shell 起点。
- 每轮保存所选计划、各 section 大小、近似 token 估算和模型实际 usage。
- usage 覆盖状态明确区分 `exact`、`partial`、`unavailable`，缺遥测不能伪装成 0。

详见 [_handbook/context-management.md](./_handbook/context-management.md)。

### 工具、Shell 与子 Agent

- 统一 Shell 生命周期支持前台/后台、PTY、`write_stdin`、增量输出、取消、超时和进程组清理。
- 每个 turn/子 Agent 有独立 execution owner；清理一个 child 不会误杀其他任务。
- inline/background 子 Agent 共用全局准入上限，结束后释放资源并回注结构化结果。
- ToolMeta 记录风险、是否常驻、能否预加载、搜索提示和 builtin/MCP/plugin 来源。
- MCP/plugin 工具可以 deferred，通过 `tool_search select:<name>` 解锁。

### Scheduler

- 支持 `at`、`after`、`every`、5/6 段 cron 和 IANA 时区。
- `instant` 直接执行；`soft` 使用隔离的 `scheduler:<job-id>` session 发起被动 turn。
- soft turn 默认跳过普通会话的记忆摄入/检索和消息推送工具，避免污染用户上下文。
- 持久化任务有容量上限，interval/cron 可重复，旧 `run_at/delay/repeat` API 继续兼容。

### 记忆

- `core/memory/` 是统一协议与服务入口；`memory2/` 是默认结构化记忆算法；具体引擎位于
  `plugins/default_memory/` 与 `plugins/akasha/`。
- Default 引擎支持结构化摄入、向量/关键词多 lane、RRF、预算注入、证据与变更操作。
- Akasha v1 仍是正式可选引擎：以完整 turn 为真相，支持图扩散/RAR、持久化、证据、召回与强化。
- 本阶段不强制升级 Akasha v2；其恢复/重建等更厚合同保留为后续扩展空间。

详见 [docs/MEMORY_SYSTEM.md](./docs/MEMORY_SYSTEM.md)。

### Proactive / Drift 与运行轨迹

- 主动链有 Gate、模块 DAG、能力快照租约、alert/content/context、冷却、投递去重和 ACK。
- `proactive.db` 保存 `tick_log` 与 `tick_step_log`，每一步记录状态、耗时、terminal 与错误。
- 被引用的 content 会向源端回传 `interesting`；失败反馈进入 pending 后重试。
- 一般 Agent turn 同样保存 `tool_chain`、`context_trace`，控制面发布 turn/tool/stream 事件。
- 当前仓库提供的是**轨迹数据与查询基础**；新的前端可视化页面暂不在本轮范围。

### Workspace 与质量门禁

- 启动时用 workspace 单实例锁保护迁移与运行；Yoyo 是统一迁移账本，migration 只允许追加。
- `scripts/check_change_impact.py` 按高风险 owner 选择语义合同测试。
- `tests/semantic/` 固化“历史不可破坏”和“Reference 可删除”两条底线。
- 主模型与轻模型可以分开配置；轻模型只在可重试传输/限流/服务端错误且尚未产生可见流时回退主模型。

## 验证

```bash
uv run pytest -q
uv run kirakira-impact --base HEAD --run
uv run kirakira-verify-online
```

`kirakira-verify-online` 使用隔离临时 workspace，不打印密钥，实际检查：

- 模型文本与具名强制工具调用；
- token usage 归一化；
- embedding；
- Runtime 真实执行并消费 `read_file`；
- 长历史投影成功且持久历史不变；
- Akasha v1 turn 摄入、召回以及模型消费召回内容。

## 目录

```text
agent/            推理、生命周期、工具、MCP、插件、调度、子 Agent
bootstrap/        组合根、初始化、supervisor、控制面与 dashboard 装配
bus/              消息队列和事件合同
core/             共享 schema、网络与记忆协议/runtime
infra/            provider、渠道、控制与持久化 adapter
session/          权威 Session 与消息 embedding store
memory2/          默认结构化记忆算法
plugins/          Default/Akasha、Proactive、Drift 等一方实现
plugin_packages/  可分发插件包
proactive_v2/     主动内核、frame 与 tick 编排
frontend/         TUI 与本地 Web 表现层
eval/             记忆评测
migrations/       append-only workspace migration
scripts/          迁移、change-impact、在线验证
kirakira_agent/   仅公开 `python -m kirakira_agent` 入口壳
```

workspace 解析顺序为：`--workspace` → `KIRAKIRA_WORKSPACE` →
`config.toml [runtime].workspace` → 当前目录。

## 文档

从 [docs/INDEX.md](./docs/INDEX.md) 开始。当前仍保留的差异与明确延期项见
[docs/NOW.md](./docs/NOW.md)；完整能力审计见
[docs/DIFFERENCE_AUDIT.md](./docs/DIFFERENCE_AUDIT.md)；实际在线证据见
[docs/design/live-verification.md](./docs/design/live-verification.md)。

前端与移动后端暂不作为本轮 Reference 对齐目标；现有前端功能保持可用，但不据此宣称产品面完全复刻。

## License

MIT，见 [LICENSE](./LICENSE)。
