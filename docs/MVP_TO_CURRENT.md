# Kirakira Agent：从 MVP 到当前版本

> 当前版本快照：2026-07-23。Reference 固定为
> `012e37c8b51df045353972bb551d8e868ab52455`。本文只写已经进入正式启动入口并经过验证的能力；
> 源码存在但没有生产调用点的内容不算完成。

## 1. 当前结论

Kirakira 目前处于“先把基础链路跑通，再逐步加深”的阶段：

| 范围 | 当前状态 | 结论 |
| --- | --- | --- |
| 被动回复 | 已跑通，工程化基座 | Web、Telegram、QQ/OneBot、CLI 可进入同一 AgentLoop |
| 主动推送 | MVP 已跑通 | Tick、Source、判断、真实 Channel callback、Session、ACK 已闭环 |
| Drift | MVP 已跑通 | 主动空转后可执行 `SKILL.md`、使用工具、选择发送并保存连续状态 |
| Dify | 未实现 | 当前仓库没有 Dify adapter、配置、调用点或端到端测试，不能写成已跑通 |
| Telegram | Reference 对齐 | 五个 `infra/channels` 文件逐字节一致，Kirakira 差异位于文件外 binding |
| 启动/Supervisor | Reference 对齐 | `agent/supervisor.py` 逐字节一致；默认入口为 supervisor → gateway |
| Memory2 | M1 完成 | `memory2.db` 是唯一结构化 owner；M2 DefaultMemoryEngine 尚未开始 |

当前完整离线回归：

```text
257 passed, 4 subtests passed
```

真实服务也已通过当前配置启动：Supervisor 和 gateway 分离运行，readiness 为 ready，Web 与 Telegram
均完成启动。这里不把未执行的 QQ、Dify 或后续 Memory2 算法写成已验证。

## 2. 从最小 MVP 到现在

### 2.1 Function Calling MVP

最初只验证最短闭环：

```text
User → Model 选择工具 → Tool 执行 → 结果回填 Model → Final Text
```

它证明 Agent 能调用工具，但没有持久会话、并发、真实渠道、主动触发和长期记忆 owner。

### 2.2 被动链路

随后形成当前共享基座：

```text
Channel 入站
  → InboundMessage
  → MessageBus
  → AgentLoop（同 session 串行，跨 session 并行）
  → PassiveTurnPipeline
  → Memory2 检索 façade + Prompt/Context
  → Streaming Model + Tool Loop
  → Session commit
  → OutboundMessage
  → 原 Channel
```

这一阶段补齐 ToolRegistry/Executor、Hook、MCP、Session、Context budget、Streaming、Plugin、Subagent、
Schedule 和多渠道。它已经超过最小 MVP，但内部厚度仍低于 Reference 的完整 runtime。

### 2.3 主动推送 MVP

```text
后台 Tick
  → Gate（目标存在、被动链路空闲）
  → SourceRegistry.fetch_all()
  → alert/content/context 去重入库
  → alert 优先；content 由 LLM 判断
  → MessageBus.publish_outbound_and_wait()
  → 真实 Channel sender
  → sender 成功后写 Session + delivery_id
  → consume + pending source ACK
```

当前内置文件 Source，以 `<workspace>/proactive/inbox/*.jsonl` 验证 fetch/ACK。Channel 缺失或发送失败
不会提交为成功。这条链路已达到可用 MVP，但还没有 Reference 的 plugin Source、durable outbox、
多目标和完整跨崩溃恢复。

### 2.4 Drift MVP

```text
主动链路本轮没有推送
  → DriftRunner 选择 drift/skills/*/SKILL.md
  → 注入记忆、近期上下文和 continuum
  → 复用 Agent 与默认工具运行
  → message_push（可选）/ finish_drift
  → Channel 成功记 sent，否则记 silent
  → 保存 run 与 continuum
```

Drift 不是第四套执行引擎，只改变触发、system prompt 和终止合同。

### 2.5 Telegram 与启动层收口

渠道与启动不承担 Kirakira 的差异化算法，因此本轮改为直接复用 Reference 源码：

```text
Reference/infra/channels/{base,contract,reply_context,telegram_channel,telegram_utils}.py
  == byte-for-byte ==
infra/channels/{base,contract,reply_context,telegram_channel,telegram_utils}.py

Reference/agent/supervisor.py
  == byte-for-byte ==
agent/supervisor.py
```

Kirakira 的 namespace、MessageBus、SessionManager、message-push、interrupt 和 gateway readiness 映射
位于外部 compatibility/binding 文件，不再修改复制源码。Telegram 因此具备 Reference 的 polling、
白名单、去重、typing、回复上下文、图片/文档、Markdown entities、UTF-16 分段、429 重试、Conflict
停收、工具/思考 live preview 和最终消息收口。

QQ/OneBot 与官方 QQBot 尚未按同一标准逐字节复刻；它们当前仍是可运行的 Kirakira 实现。

### 2.6 Memory2 M0–M1

M0 建立 doctor、依赖/import/schema/Reference 漂移检查和基础契约测试。M1 完成 owner 切换：

```text
memory/memory2.db          唯一结构化长期记忆 owner
memory/MEMORY.md           独立人工长期档案
memory/SELF.md             独立自我模型
memory/PENDING.md          独立待整理事实
memory/RECENT_CONTEXT.md   可重建投影
```

旧 `items.json` 不再参与正式读写；被动链路、记忆工具和 Dashboard 通过同一个 Memory2 兼容 façade。
迁移使用 offline lock、backup、staging DB、integrity check、原子发布和 rollback。

M1 只解决唯一 owner 和可恢复迁移，不等于 Memory2 算法已经对齐。详见
[MEMORY2_M0_M1.md](./MEMORY2_M0_M1.md)。

## 3. Memory2 长期计划进度

| 里程碑 | 状态 | 已完成/下一步 |
| --- | --- | --- |
| M0 可执行差距审计 | 完成 | doctor、Reference pin、import/schema/count/vector/Markdown 检查 |
| M1 唯一结构化存储 | 完成 | staging 迁移、唯一 owner、兼容 façade、Dashboard、verify/rollback |
| M2 DefaultMemoryEngine | 未开始 | Memorizer、工具新 schema、embedding 向导/backfill、显式检索语义 |
| M3 被动/主动检索切换 | 未开始 | `engine.query()`、类型阈值、scope、timeline/interest、recall inspector |
| M4 自动写入与四文件链路 | 未开始 | Reference consolidation、PostResponseWorker、optimizer |
| M5 证据/撤销/持久化收口 | 未开始 | source_ref 回源、session undo、replacement 恢复、并发边界 |
| M6 评测与切除旧链路 | 未开始 | LongMemEval、状态 oracle、删除兼容 façade 和旧 consolidation |

下一次记忆升级必须从 M2 开始，不得跳过 embedding 配置、Reference engine 接线和真实生产调用验证。

## 4. 当前启动方法

首次配置：

```bash
uv run python main.py setup
```

没有 `config.toml` 时，直接运行也会进入向导：

```bash
uv run python main.py
```

正式启动链：

```text
main.py
  → entry
  → Reference supervisor（workspace lock / boot_id / signal / readiness）
  → main.py gateway
  → Runtime + configured Channels + Proactive + Drift
```

常用命令：

```bash
uv run python main.py                 # 正式 supervisor 入口
uv run kirakira-agent                # 同一正式入口
uv run python main.py gateway         # 未托管调试入口
uv run python main.py init            # 非交互初始化
uv run python main.py memory doctor   # Memory2 只读检查
uv run pytest -q                      # 完整离线回归
```

启动和渠道详细合同见 [STARTUP_AND_CHANNELS.md](./STARTUP_AND_CHANNELS.md)。

## 5. 当前明确没有完成的部分

- Dify 链路不存在；下一步若要接入，必须先定义 adapter、配置、入站/出站 owner 和端到端验收。
- QQ 两种渠道能运行，但尚未像 Telegram 一样直接复用 Reference 源文件。
- 主动链路仍是 MVP：缺少真实 plugin/MCP Source、多目标、durable outbox 和完整恢复语义。
- Drift 缺少 Reference 的 journal、self-observation、hazard drive 和完整 lifecycle。
- Memory2 只到 M1；embedding 未配置，M2–M6 均未完成。
- Reference 的完整 app-server、控制协议、插件市场和完整 Dashboard 不在当前版本。

## 6. 下一次工作的优先级

1. 先选一个尚未跑通的基础链路：Dify 或 QQ Reference 对齐，并给出真实端到端证据。
2. 记忆系统从 M2 开始，接入 DefaultMemoryEngine 和 embedding，不跳阶段。
3. 再根据真实使用中的漏发、误发、重复和打扰问题加深主动/Drift，而不是先增加抽象。

## 7. 文档导航

- [STARTUP_AND_CHANNELS.md](./STARTUP_AND_CHANNELS.md)：启动、Telegram 与其他渠道现状。
- [MEMORY2_M0_M1.md](./MEMORY2_M0_M1.md)：Memory2 M0/M1 owner、数据流、迁移和恢复。
- [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md)：Kirakira 与 Reference 的当前差异。
- [VERSION_EVOLUTION.md](./VERSION_EVOLUTION.md)：被动链路的演进历史。
- [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md)：主动推送与 Drift 架构。
