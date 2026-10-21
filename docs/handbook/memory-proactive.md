# 记忆、主动交互与升级

所有命令使用项目同一个 `.venv` / Python 3.14.6。运行数据和模型缓存放本机磁盘。
配置前停止该工作区的实例，实例锁阻止并发修改。

## 升级既有工作区

```sh
uv sync --frozen --group dev --group eval --extra providers
uv run --group eval kirakira-agent upgrade --workspace /local-disk/workspace
```

upgrade 用 prepare/CAS 刷新当前已选内置插件输入，并加入 runtime_diagnostics、plugin_update、
local-embedding、event-ingress；其余启停选择及市场安装输入保留。
原 bundle.patch.toml、plugin-stable.json 与选择切换回执备份到工作区 operator-backups/。
新建工作区已选中这些提供方。旧格式数据先按[迁移指南](start.md)转换，不能直接升级。

## 本地记忆

```sh
uv run --group eval kirakira-agent configure-memory \
  --workspace /local-disk/workspace --cache /local-disk/models --download
```

首次显式下载 BAAI/bge-small-zh-v1.5（512 维中文向量），生成权重和 tokenizer 摘要清单；
不加 --download 则只使用已有本地资产。配置通过 Models RPC 设置默认 embedding，
并启用 Akasha。此后编码仅使用本地 CPU，不请求任何现金 embedding 接口。
聊天模型与本地向量模型互相独立，记忆查询不要求指定聊天供应商。

Akasha 从原始完整闭段学习；Markdown 档案与图记忆分别拥有自己的派生表示。
不要用 SQLite 手动插入记忆或改消息正文。已经保存的模型空间不能静默换成另一种维度或权重。
迁移前的旧闭段是否纳入学习由 cutover/消费规则决定，配置成功不等于历史全量重建。

## 本地主动目标

希望直接从对话订阅项目变化时，使用工作台的[跟进](followups.md)：支持 RSS / Atom 与 GitHub 发布源，并可核对调查、原文和投递确认。下面的事件入口供其他本机集成使用。

先在工作台创建或使用一个本地会话，再配置：

```sh
uv run --group eval kirakira-agent configure-proactive \
  --workspace /local-disk/workspace --session akashic:my-session --timezone Asia/Shanghai
```

命令启用 Wake，投递只指向该本地会话。外部渠道需要各自账号和提供方。
启动 gateway 后，本地集成可经已认证 JSON-RPC 调用以下方法；事件来源固定，调用方不能冒充其他插件：

| 方法 | 参数与用途 |
| --- | --- |
| event-ingress/content | batch_id、items；每项 item_id、revision、payload，交给兴趣池和 Wake |
| event-ingress/alert | event_id、payload、observed_at，可选 expires_at；显式事件通知 |
| event-ingress/context | 同上；更新有期限背景，不直接要求发送 |
| event-ingress/unsettled | 空对象；读取本来源待确认的内容结算 |
| event-ingress/ack | settlement_ref；确认内容结算 |

时间使用带时区的 ISO8601。相同 Alert event_id 与 observed_at 重投复用 mail_id，
accepted=false 表示已有该版本；不能把整个响应字典相同作为去重条件。
Content 接纳不意味着一定分享：兴趣不足会留下 content_insufficient，发送成功以 Delivery 回执为准；delivered 表示渠道确认，不代表用户已经阅读。
现有 Drift 负责提案状态和结算；自动生成空闲提案的生产者仍是[剩余项](../status/roadmap.md)。

## 复现评测

```sh
uv run --group eval python -m eval.memory_runtime \
  --workspace /local-disk/fresh-memory-eval \
  --model-config /private/path/model.json --key-file /private/path/model.key \
  --embedding-manifest /local-disk/models/embedding-BAAI--bge-small-zh-v1.5.json \
  --output /local-disk/memory-runtime.json
```

必须使用全新隔离目录。结果记录分母、原始失败和模型条件；原始运行日志仅留私有目录，
不提交初始网页登录凭据。小样本受控对比用于检查功能，不能当作公开数据集或其他系统的排名。
公开历史、无记忆/向量/混合/图式检索对照及运行方法见[评测说明](../../eval/README.md)。

这个可复现的功能场景先在真实渠道写入项目决定、更正和相似项目干扰，再让新事件触发调查与投递；事件本身不含发布时间、负责人和数据约束的答案。另一个场景把签字人放在两万多字符历史消息的末尾，要求 Agent 从召回片段继续读取原文后回答。它们检查完整工作流，和固定材料预算的公开质量实验分开计数。

本机集成可直接使用同一环境中打包的 SDK，私有 Unix socket 由文件权限保护；
配置 loopback TCP 时需按 SDK 合同提供 workspace token：

```python
from datetime import UTC, datetime
from kirakira_agent.client import AsyncClient

async def report():
    async with await AsyncClient.for_workspace("/local-disk/workspace") as client:
        return await client.request("event-ingress/alert", {
            "event_id": "instrument-offline-001",
            "observed_at": datetime.now(UTC).isoformat(),
            "payload": {"title": "仪器离线", "content": "请检查本机连接线。"},
        })
```

更多连接、订阅与令牌合同见 [SDK 文档](../../sdk/python/README.md)。

已有消息若处于首次启用前的 cutover 边界，不会因为设置 embedding 自动重新学习。
 reindex 只复用已缓存的向量，缺失时会明确跳过。需要纳入尚未编码的旧历史，先显式补算本地向量：

```sh
uv run --group eval kirakira-agent configure-memory \
  --workspace /local-disk/workspace --cache /local-disk/models --backfill-history
```

补算只读取 eligible 会话的完整闭段，遵守 suppress 标记，写入 Ledger 派生缓存。
随后在本地会话执行命令：

```text
/akasha_reindex confirm
```

该命令沿正式来源与 Commands 执行，从 canonical 消息重建派生图并保留恢复点，
不修改原始消息或重放历史工具效果。恢复完整工作区时，还需保持 embedding 资产清单与权重摘要一致。
