# Kirakira Agent：从 Function Calling MVP 到被动式 Agent Runtime

> 本文只讲**用户消息触发的被动链路**：它如何从最小 Function Calling 闭环演进为可长期运行的
> Agent Runtime。主动推送与 Drift 是后来新增、由后台时钟触发的另一条架构线，见
> [PROACTIVE_ARCHITECTURE.md](./PROACTIVE_ARCHITECTURE.md)。两条线分开记录，避免把“被动基座如何长成”
> 和“Agent 如何在没有用户消息时行动”混成一条时间线。

## 1. 为什么从 MVP 讲起

这个项目最有价值的部分，不是最终堆出了多少模块，而是一个最小 Agent 如何在真实问题推动下逐步工程化：

```text
MVP：模型能调用一个工具
  ↓
工具执行可靠：参数、错误、超时、上下文都可控
  ↓
工具规模扩大：动态发现、延迟加载、MCP、Hook
  ↓
对话可持续：Session、历史回放、长期记忆
  ↓
系统可运行：MessageBus、并发、Channel、Streaming
  ↓
系统可扩展：Plugin、Subagent、Schedule
  ↓
上下文可治理：PromptBlock、预算预检、语义降级、trace
  ↓
追平 reference：对齐工具、迁移 memory2 核心状态与行为、按运行时分层搬 skill
  ↓
四个地基一次做完：异步 runtime、依赖注入、Turn+slot 图、记忆 seam（第 12 节）
  ↓
可扩展与可运维：插件代际热重载、主动链路 lifecycle 化、控制面（第 13 节）
  ↓
当前：完整被动式 Agent Runtime + 可程序化驱动
  ↓
下一版：评测驱动的工具编排与回归体系
  ↓
后续：100–200 用户、多租户和后台管理
```

**为什么第 12 节是一道分水岭**:前十一节都是"遇到问题补一层",到第七轮末尾出现了
新症状——**补不动了**。那说明剩下的问题不再是"少了功能",而是"抽象的形状不对"。
把"该加代码"和"该动地基"分开判断的方法,单独写在
[ENGINEERING_METHOD.md](./ENGINEERING_METHOD.md)。

下面的“版本”是工程演进阶段，不是为了包装而虚构的发布标签。每一阶段都回答四个问题：最小目标是什么、为什么要加下一层、实际解决了什么、还留下什么问题。

> **配套阅读**:本文是编年史(先后发生了什么)。把这些经历提炼成可迁移判断的,
> 是 [ENGINEERING_METHOD.md](./ENGINEERING_METHOD.md)——它讲复杂系统的五种腐坏方式、
> 怎么判断该重构还是该加功能、以及"测试绿 ≠ 系统能跑"这一课的代价。

## 2. MVP：先让模型正确调用工具

### 2.1 最小可行目标

MVP 只验证一件事：模型能否根据用户请求选择工具，Harness 能否执行工具，并将结果以正确协议送回模型。

```text
User Message
    ↓
LLM 返回 ToolCall(name, arguments)
    ↓
ToolRegistry 查找并执行 Handler
    ↓
ToolResult 回填 messages
    ↓
LLM 生成最终回复
```

最初只需要：

- 一个 OpenAI-compatible 模型客户端。
- 一个 `ToolSpec`，描述 name、description 和 JSON schema。
- 一个 `ToolRegistry`，保存 spec 与 handler。
- 一个循环：模型回复工具调用就执行，否则结束。
- `bash`、`read_file`、`write_file` 等少数基础工具。
- CLI 输入输出。

### 2.2 MVP 为什么是“可用”而不是“完整”

它已经能完成“读取文件并解释”“运行命令并总结”这类任务，因此验证了产品核心假设。但它只能服务一个本地会话，错误处理和状态管理非常薄弱。

### 2.3 MVP 暴露的问题

- 模型可能传错参数、漏参数或调用不存在的工具。
- 工具异常可能直接打断整个循环。
- Shell 可能超时，子进程可能残留。
- Tool Result 太长会撑爆上下文。
- 多个工具越来越难全部暴露给模型。
- 没有 trace，出了问题只能猜是模型、工具还是协议错了。

因此第一轮工程化不应马上做复杂记忆，而应先把工具执行链路跑稳。

## 3. 第一轮工程化：让工具执行不出错

### 3.1 从函数调用升级为执行管线

工具真实链路演进为：

```text
Reasoner
  -> ToolExecutor.execute(request, ToolRegistry.execute_async)
      -> pre hooks
      -> registry dispatch
      -> schema validation（校验 Hook 改写后的最终参数）
      -> handler / API / subprocess
      -> post success or post error hooks
      -> normalized ToolResult
  -> append tool message
  -> next LLM iteration
```

### 3.2 ToolRegistry 的职责

ToolRegistry 不只是字典，而是统一工具目录和执行边界：

- 保存工具 schema 与 handler。
- 向模型暴露可见工具定义。
- 执行前校验 required、type、enum。
- 支持同步和异步 handler。
- 同步阻塞工具通过 worker thread 执行，避免卡住 event loop。
- 用 `ContextVar` 保存当前 session/channel/chat，避免并发串线。
- 统一未知工具、参数错误和 handler 异常为 ToolResult。

### 3.3 ToolExecutor 的职责

ToolExecutor 将“工具能不能执行”与“工具怎么执行”分离：

- pre hook 可以改参数或拒绝执行。
- pre hook 异常 fail-closed，避免安全模块失效后继续执行。
- post hook 记录结果或追加上下文。
- timeout 和取消统一变成错误结果。
- lifecycle 事件记录工具开始、结束、状态和耗时扩展点。

### 3.4 文件、Shell 和网络工具的边缘治理

- 文件路径必须位于 workspace 内。
- 写文件使用临时文件 + replace，避免半写入。
- edit 在多个匹配时拒绝默认替换，防止误改。
- 文本读取检查二进制 NUL。
- Shell 使用独立进程组，timeout/cancel/shutdown 都清理子进程树。
- 长任务可转后台，通过 `task_output` 轮询、`task_stop` 终止。
- `web_fetch` 检查 DNS/IP 和每一次 redirect，阻止 SSRF 访问私网。
- 网络响应限制类型和大小。

### 3.5 这一阶段解决了什么

Agent 不再只是“模型说调用就调用”，而是在 schema、权限、Hook、超时和标准化错误之内执行。工具问题可以被定位到参数校验、pre hook、handler、post hook或模型下一轮决策。

## 4. 第二轮工程化：工具多了以后怎么管理

### 4.1 为什么不能把所有 schema 永远塞给模型

工具数量增长会带来三个问题：

- schema 占用上下文，增加 token 与延迟。
- 相似工具变多，模型更容易误选。
- MCP 或插件工具运行时才出现，静态列表无法覆盖。

### 4.2 延迟加载与动态发现

当前实现将工具分为 always-on 和 deferred：

1. 首次请求只暴露核心工具和 `tool_search`。
2. 模型调用 `tool_search` 检索目录。
3. `select:<tool_name>` 解锁目标工具。
4. 下一轮模型请求才注入该工具 schema。
5. 每个 session 使用 5 项 LRU，避免已解锁工具无限增长。
6. 模型绕过搜索直接调用隐藏工具时，Reasoner 拒绝执行并返回选择提示。

### 4.3 MCP 接入：先用命令式注册

MCP 接入不是另写一套执行器，而是适配到统一 Registry：

```text
mcp_add（模型调用的工具）
  -> 启动 stdio MCP server
  -> initialize
  -> tools/list
  -> 将远端 schema 转成 ToolSpec
  -> 注册到 ToolRegistry（deferred）

模型调用远端工具
  -> ToolExecutor -> ToolRegistry -> tools/call JSON-RPC -> ToolResult
```

这样本地工具、插件工具和 MCP 工具共享 schema 校验、Hook、生命周期、错误语义和可见性策略。

> **这一版后来被推翻了。** 命令式注册有三个洞：状态只存在于“谁调用过 mcp_add”，重启后要靠
> `mcp_servers.json` 回放；一个 server 连不上时，前面已经连上的处于半完成状态；改配置要模型
> 自己记得调 `mcp_remove` + `mcp_add`。第 8 节会讲我们怎么换成声明式，以及为什么这是同一类
> 问题的通用解法。

### 4.4 插件与 Hook

插件可以：

- 注册新工具。
- 通过 pre hook 阻断或改写工具。
- 在 turn phase 注入上下文或提前返回。
- 声明 skills 和 MCP servers。
- 使用独立配置、KV 和 data dir。

插件加载失败会撤销已经注册的资源，单个坏插件不会阻止 Runtime 启动。

早期版本用描述符文件 `.aka-plugin/plugin.json` 声明这些能力（名字、lifecycle entry、skills
路径、MCP 配置路径）。这一版同样在第 8 节被推翻：能力改为由插件根目录的 `plugin.py` 用代码
声明，全局清单只保留“启用与否”。

### 4.5 下一步编排空间

当前编排核心仍由模型自由选择工具，工程控制主要是可见性、权限和循环保护。下一版应进一步加入：

- capability policy，而不是维护 disabled tool 名单。
- read/write/network/process/admin 风险分级。
- 对高风险工具增加 approval 或 policy gate。
- 记录工具选择、参数修复、重试、fallback 和终止原因。
- 建立工具路由评测集，判断模型是否选对工具、参数是否正确。

## 5. 第三轮工程化：从单轮工具 Agent 到长期对话

### 5.1 Session 先解决短期状态

Session 保存用户消息、助手回复、reasoning、tool calls 和 tool results。重建模型历史时，必须展开成协议正确的 assistant/tool 消息序列，并从 user boundary 开始，避免孤立 tool message。

当前 Session 使用：

- JSON 作为 canonical store。
- 临时文件 + `os.replace` 原子保存。
- 可读 key + hash 防止文件名清洗碰撞。
- SQLite FTS5 trigram 作为可重建的消息搜索索引。
- `search_messages` 返回 source_ref，`fetch_messages` 回源 JSON。

### 5.2 为什么短期历史不等于长期记忆

完整历史直接塞入 Prompt 会持续增长，也无法区分稳定偏好和一次性内容。长期记忆需要独立处理：

- 哪些内容值得保存。
- 内容属于身份、偏好、流程还是事件。
- 如何去重、强化、更新和遗忘。
- 回答当前问题时是否需要召回。
- 召回结果如何追溯到原会话。

### 5.3 当前记忆写入链路

```text
Turn committed
  -> 同步写 RECENT_CONTEXT / HISTORY
  -> 回复先返回用户
  -> 后台 consolidation worker
  -> LLM 从窗口对话中抽取结构化 memories/history
  -> exact dedup + reinforcement
  -> 写 items.json 和 MEMORY.md 托管区
  -> 更新 last_consolidated
```

同 session 下一轮开始前会等待上一轮 consolidation 收口，避免边写边读。Session 删除时，带 source_ref 的记忆会被撤销，避免“对话删了，事实还在”。

### 5.4 第一版检索：加权融合，以及它为什么是错的

第一版是这样融合的：

```python
score = semantic * 0.75 + lexical * 0.25
if query in record.content.lower():
    score += 2.0
```

它跑得起来，也确实能召回，所以问题被掩盖了很久。但它有一个根本缺陷：
**把两个尺度不可比的原始分数直接相加**。

- cosine 大致落在 [-1, 1]，而且实际语料里常常挤在 0.7~0.95 这个窄带。
- 词法分是归一化重叠率，落在 [0, 1]，分布形状还随 query 长度变化。

两者相加得到的数没有统一含义，所以 0.75 / 0.25 这组权重是**不可证伪**的——调大调小都能编出
理由，因为没有任何一个尺度能说明"多 0.1 意味着什么"。那个 `+2.0` 更是直接盖过前面所有项。

但真正的伤害不是排序，而是**准入**：

> 每条记录都能算出一个非零分，所以 `limit` 永远会被填满。

实测：查一个精确品牌名 `Orijen`，旧算法除了正确那条，还会带出一条**和 query 一个词都不重合**
的记忆——它纯粹靠 `semantic * 0.75` 从一个不相关的 cosine 里拿到了分数。于是每一轮 prompt 里
都被塞进无关记忆，而无关记忆会实实在在地拉低回答质量。

### 5.5 第二版：多路召回 + RRF

```text
query
  ├─ lexical lane   变量名、命令、路径、错误码、精确实体
  └─ vector  lane   口语化、同义改写、"上次说的那个东西"
        ↓ 各 lane 内部独立排序，各自决定谁有资格进来
      RRF 融合（只看 rank，不看原始分）
        ↓
      热度加权（强化次数随时间半衰）
        ↓
      注入预算（字符/行数硬上限）
```

RRF 的公式：

```text
score(item) = Σ_lane  weight_lane / (k + rank_in_lane)
```

关键在于**它只用名次**。因此每条 lane 只需要"自己内部排得对"，跨 lane 根本不需要可比——
上一节那个尺度问题就此消失。`k = 60` 是文献常用值，作用是压平头部差距。

准入问题也一并解决了：每条 lane 有自己的准入规则（词法要求 overlap > 0，向量要求
cosine ≥ 阈值），**不匹配的记录是缺席，而不是排在最后**。同样查 `Orijen`，新实现只返回
一条。

两条 lane 的分工是互补的，这正是要融合而不是二选一的原因：向量认得"kitten ≈ feline"，
但认不得 `scripts/rollout.sh`；词法反过来。

热度加权用半衰期 14 天：强化次数的加成会随时间衰减，否则一条被反复提到的旧记忆会**永远**
压住新记忆。

### 5.6 还没做的，以及为什么

reference 的 `memory2` 还有 query rewrite、HyDE、sufficiency checker。这些**故意没做**：

每一个都要在**每轮对话里多打一次 LLM**。这是实打实的延迟和成本，而收益未经测量。
reference 自己也用配置门控它们。

判断标准很简单：**RRF、热度、注入预算都是纯计算，零额外网络往返，所以先做；凡是要多花一次
模型调用的，必须先有评测集能证明它值。** 不能因为"HyDE 听起来高级"就默认开启。

`retrieval.py` 里的 `MemoryRetrievalPipeline` 协议就是为这一步留的接缝：要换整套检索策略，
实现这个协议即可，被动链路不用动。

### 5.7 仍然缺的（此节已被后续升级覆盖）

query rewrite、语义去重、矛盾处理（同一事实的新旧版本冲突）、图关系存储。

> **更新(2026-07-25)**:上面 5.4–5.7 描述的是第一/二版**词法为主的检索**。此后记忆已照 Reference
> 重建为 `DefaultMemoryEngine`(装配 HyDE / query rewrite / 语义去重 / 替换 / 自动摄入),并通过
> `MemoryServices` 依赖注入接入被动检索——见第 11.5–11.7 节与 [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md)。
> `retrieval.py` 留的那条接缝正是为此:现在被动 turn 调 `engine.query(MemoryQuery(intent="context"))`,
> 检索智能移到引擎后面,runtime 不再感知。承重需配 `[memory.embedding]`。

## 6. 第四轮工程化：多渠道、并发与异步链路

### 6.1 为什么引入 MessageBus

CLI、Web、Telegram 和 QQ 都需要相同 Agent 行为。如果 Channel 直接调用模型，session、记忆、错误和流式逻辑会复制多份。

统一链路为：

```text
Channel -> InboundMessage -> MessageBus -> AgentLoop
        -> PassiveTurnPipeline -> OutboundMessage -> Channel
```

### 6.2 并发模型

- 同 session 串行，避免历史交叉写入。
- 不同 session 并行，避免慢用户阻塞其他会话。
- 同 chat outbound ticket 保序。
- 不同 chat 并发发送。
- Web request 使用 correlation id，防止同 session 并发请求拿错回复。
- `/stop` 取消 active turn，并保存 partial reply/thinking/tool chain。

### 6.3 Streaming

模型 SSE 被解析为正文、reasoning 和 fragmented tool calls。Stream delta 通过 lifecycle event 发给 Channel：Telegram 先发送占位消息再 edit，Web 通过事件接口获取进度，最终回复仍走统一 OutboundMessage。

### 6.4 资源生命周期

正常关闭需要按顺序处理 subagent、AgentLoop、scheduler、bus drain、Channel、后台 Shell、插件、MCP、memory worker、EventBus 和 Session index。只停止主循环会留下子进程、后台任务或未完成队列。

## 7. 第五轮工程化：Subagent、Schedule 与可扩展 Runtime

### 7.1 Subagent

- inline 模式阻塞当前 turn，结果直接作为 ToolResult。
- background 模式立即返回 task id，完成后回注原 session。
- 独立 session 隔离上下文。
- research/scripting/general profile 控制工具权限。
- 禁止递归 spawn、发消息、改 MCP/插件和创建 schedule。
- 最大并发 3，支持 list/cancel。

### 7.2 Schedule

Schedule 只执行用户明确创建的定时消息，不包含自主决策。任务持久化 fire time、interval 和 status，到期后通过 MessageBus 发往原 Channel。

### 7.3 当前完整版本

截至当前，项目已经具备：

- Web、Telegram、QQ/OneBot、CLI。
- session-aware AgentLoop 和 streaming tool loop。
- ToolRegistry、ToolExecutor、Hook、deferred discovery 和 MCP。
- Session、FTS 消息搜索、长期记忆和后台 consolidation。
- Plugin、Subagent、Schedule 和 graceful shutdown。
- PromptBlock、Context Frame、Provider 预算预检、语义降级与持久化 context trace。
- 自动化回归覆盖工具、Session、并发、MCP、snapshot、上下文、记忆与多渠道。
- DeepSeek 在线普通响应、真实工具调用、记忆抽取和 context usage/baseline 验证。

这已经是完整被动式 Agent Runtime，但还不是面向多租户和 SLA 的生产平台。

## 8. 第六轮工程化：当运行时开始"边跑边改"

前面五轮有一个共同的隐含假设：**能力集合在进程启动时确定，之后不变**。工具就在那儿，MCP
连上了就一直连着。这一轮打破了这个假设，而它打破的方式很值得学——因为它不是"加一个功能"，
而是发现了一整类之前看不见的 bug。

这一轮的起点很朴素：把 reference 更新到最新，看看差在哪。结果发现 reference 把我们照抄过来的
两个子系统整个推翻重做了。与其照抄新版本，不如先搞清楚**它为什么要推翻**。

### 8.1 命令式 vs 声明式：MCP 的重做

旧设计（我们抄来的那版）里，MCP server 靠模型调用 `mcp_add` / `mcp_remove` 来管理，配置落在
`mcp_servers.json`。新设计把这些工具全删了，改成声明式：

```text
workspace/mcp/
├── servers/
│   └── fitbit.toml        ← 一个文件一个 server，文件名必须等于 name
└── fitbit-mcp/
    └── run_mcp.py
```

```toml
schema_version = 1
name = "fitbit"
command = ["python", "run_mcp.py"]
cwd = "../fitbit-mcp"
watch_paths = ["../fitbit-mcp/run_mcp.py", "../fitbit-mcp/src"]
```

这个转变背后是一个通用的架构判断，值得单独记住：

| | 命令式（旧） | 声明式（新） |
| --- | --- | --- |
| 真相在哪 | "谁调用过哪些命令"的累积结果 | 文件内容本身 |
| 重启后 | 回放 json 才能恢复 | 读一遍文件即可 |
| 改配置 | 记得先 remove 再 add | 改文件 |
| 部分失败 | 前几个连上了，第四个炸了 → 半完成状态 | 整批拒绝，旧代际继续服务 |
| 谁负责收敛 | 调用方 | watcher（对比期望与实际） |

**命令式描述"怎么变"，声明式描述"应该是什么"。** 只要"应该是什么"能被完整写下来，声明式
几乎总是更好：它天然幂等、天然可重启、天然可 diff。这是 Kubernetes、Terraform、Nix 共用的
同一个思路，reference 只是把它用在了 MCP 上。

我们的实现分三块，职责边界很清楚：

```text
declarations.py   只回答"期望状态是什么" —— 解析 + 校验 + 算内容 revision
host.py           只回答"能不能连上" —— 整批连接，任一失败清理整批
watcher.py        只回答"什么时候该换" —— 轮询 revision，串行发布
```

`revision` 是内容哈希，不是 mtime——`touch` 一下文件不会触发重连，真改了内容才会。watch_paths
让 server 的源码变化也能触发换代（连 hash 都覆盖了目录下所有文件）。

### 8.2 为什么"整批拒绝"比"尽力而为"好

`host.prepare()` 里有个刻意的选择：任何一个 server 连不上，**已经连上的那些也全部断开**，
整批候选作废。

直觉上这很浪费——三个连上了，第四个坏了，为什么不留下那三个？

因为"部分可用"是最难排查的状态。留下三个的话，模型会看到一个残缺的工具集，然后给出一个
**看起来正常但其实基于不完整能力**的回答。用户不知道第四个 server 没连上，只觉得"agent 今天
有点笨"。而整批拒绝 + 旧代际继续服务的结果是：要么全新，要么全旧，永远是一个自洽的集合。

这就是 reference 那次 236 文件大重构（#111 "fail-loud runtime contracts"）的核心思想。它可以
浓缩成一句话：

> **能降级的地方降级，不能降级的地方必须报错。静默吞掉失败会制造"看起来正常、其实已经损坏"
> 的状态，而这种状态的排查成本远高于当场崩溃。**

我们没有照抄那 236 个文件（其中大半是我们不做的 proactive/dashboard），而是把这条规则用在
自己的被动链路上。最能说明问题的是记忆的向量写入：

```python
def _embed_for_query(self, text):
    # 检索侧可以降级：拿不到向量就退回词法召回，本轮仍然有答案。
    except Exception:
        logger.exception("embedding failed; falling back to lexical recall")
        return None

def _embed_for_store(self, text):
    # 写入侧不能降级：静默存入无向量记录，会让这条记忆此后永远无法被语义召回。
    except Exception as exc:
        raise RuntimeError(...) from exc
```

同一个 `embed()` 调用，同一个异常，两个相反的处理。差别不在于"错误严不严重"，而在于
**降级之后的状态是否还是自洽的**：

- 查询失败 → 这一次答案差一点，下次向量服务恢复就好了。**没有留下痕迹。**
- 写入失败 → 这条记忆永远缺向量，索引里一半有一半没有。**损坏被固化了。**

判断准则：问"如果我在这里静默返回默认值，半年后谁会为此调试到凌晨三点？"

### 8.3 代际快照与租约：本轮最重要的一课

声明式热重载带来一个新问题，而这个问题在旧的命令式设计里同样存在、只是没人注意到。

考虑这个时序：

```text
t0  turn 开始，模型准备调用 mcp_fitbit__today
t1  你改了 fitbit.toml，watcher 决定换代
t2  watcher 从 registry 里注销旧工具、断开旧进程
t3  模型真的发出 mcp_fitbit__today 调用
    → "Unknown tool"，或者更糟：拿着已经断开的连接去调用
```

只要工具挂在**共享可变**的注册表上，这个竞态就无法回避。加锁也没用——你不能让一个可能跑
几十秒、十几轮工具调用的 turn 一直持着锁不放。

reference 的解法是**把"当前能力"从一个可变对象变成一串不可变代际**：

```text
RuntimeSnapshotStore
├── current              新 turn 用哪一代
├── publish/commit       换代是事务：候选先就绪，再切 current，失败可回滚
└── retire + drain       旧代际退休后，等最后一个租约释放才真正销毁资源

RuntimeSnapshotLease
└── turn 开始时取一份，整个 turn 都看同一份能力集合
```

关键在于：**换代只切换 `current` 指针，不动任何在途 turn 看到的东西。**

- 新 turn → 立刻用新能力。
- 在途 turn → 继续用它开始时锁定的那一代，包括那一代的 MCP 连接。
- 旧进程 → 等最后一个租约释放（`lease_count` 归零）才断开。

我们的实现里有一个具体决定：**MCP 工具不再进共享 ToolRegistry，只挂在快照上**。基础注册表
只放启动即固定的内置/插件工具，会变的东西挂在快照。这样 `SnapshotToolView` 把两者组合成
本轮的只读视图：

```python
tools = SnapshotToolView(self.tools, get_current_runtime_snapshot())
```

这行在 reasoner 每轮开头执行一次，之后整轮都用它。

这个设计的正确性有一个测试专门盯着（`test_turn_pins_snapshot_tools_across_mid_turn_hot_reload`）：
turn 中途真的换代，然后断言本轮仍然能调用旧代际的工具、拿到旧代际的返回值，而全局 `current`
已经是新代际，并且旧代际在本轮租约释放后才 `drained`。

### 8.4 ContextVar 绑定为什么要检查 owner task

快照通过 ContextVar 绑定到当前 turn：

```python
def get_current_runtime_snapshot():
    binding = _current_binding.get()
    if (binding is None
        or not binding.lease.active
        or binding.owner_task is not asyncio.current_task()):
        return None
    return binding.lease.snapshot
```

`owner_task is not asyncio.current_task()` 这个检查容易被当成多余，但它防的是一个真实问题：
ContextVar 会被 `asyncio.create_task()` 自动继承。如果父 turn 派生了一个后台子任务，子任务
会"免费"看到父任务的快照——但它并没有自己的租约。于是父 turn 结束、租约释放、资源销毁之后，
子任务还拿着一个已经 drained 的快照在跑。

所以规矩是：**想跨任务用快照，必须自己 `fork()` 一份租约**，让 `lease_count` 如实反映真正
还在用它的人数。租约计数是资源回收的唯一依据，任何"白嫖"都会让计数说谎。

### 8.5 顺带修掉的一个真实回归

把 MCP 工具移出共享注册表之后，跑起来发现 `/tools` 不再列出任何 MCP 工具了——因为它读的是
基础注册表，而且它在任何 turn 之外执行，根本没有租约。

这个 bug 很典型：**测试全绿，但产品坏了**。132 个测试没有一个覆盖"人在 REPL 里敲 /tools"。
只有真的把进程跑起来、接上真实 stdio MCP server、敲一遍命令才会发现。修复本身是两行，但
教训是：改动只要动了"谁能看见什么"，就必须真的去看一眼。

### 8.6 派生优于硬编码：context policy

同一轮里还有一个小改动，但体现同一种思路。原来的配置是：

```toml
[agent.context]
memory_window = 40    # 为什么是 40？没人知道
```

现在改成从模型真实容量按 1M 基准等比例派生：

```python
memory_window = max(20, round(effective * 160 / reference_effective) 对齐到 4)
output_reserve = max(4096, min(32768, ...))
```

换模型时只需要写 `context_window = 128000`，历史窗口和输出预留自动跟着走。**一个硬编码常数
背后往往藏着一个没写下来的公式**；把公式写出来，常数就变成了它的一个取值。

顺带一提，reference 在 #117 把基准从 640 调到 160——一个纯参数调整。这提醒我们：派生公式
本身也是要调的，但调一个基准值比逐个模型改配置便宜得多。

### 8.7 这一轮的收获

```text
命令式 → 声明式        真相放在内容里，让 watcher 负责收敛
尽力而为 → 整批拒绝     宁可全旧，不要半新半旧
静默降级 → 分情况       降级后状态自洽才能降级，否则必须报错
共享可变 → 不可变代际   在途请求锁定一份，换代只切指针
硬编码 → 派生           把常数背后的公式写出来
```

这五条没有一条是 agent 特有的。它们是并发系统、配置管理、资源生命周期里的通用模式，只是
在这个项目里同时出现了。

## 9. 实际遇到并修复的 Bug

### 9.1 Web 并发请求串回复

问题：同一 session 同时发送两个 HTTP 请求时，只按 chat id 等待 outbound，后返回的请求可能拿到前一个回复。

修复：每个请求生成 `client_request_id`，Pipeline 将其传播到 Outbound metadata，Web 只解析匹配 correlation id 的 future。

验证：增加同 session 并发请求集成测试。

### 9.2 Outbound 队列无法 graceful drain

问题：dispatch 后未在所有路径调用 `task_done()`，关机等待 queue join 可能永久卡住。

修复：将完成标记放进 dispatch task 的 `finally`，并测试同 chat 顺序和跨 chat 并发。

### 9.3 DeepSeek 工具历史协议错误

问题：DeepSeek thinking 模式下，包含工具调用的 assistant 消息需要回传对应 `reasoning_content`；丢失后续轮次可能被 API 拒绝或推理断裂。

修复：每个 tool-chain group 持久化 reasoning，Session history reconstruction 原样恢复。

### 9.4 Session 文件名碰撞

问题：只替换特殊字符时，`a:b` 和 `a/b` 可能落到同一文件。

修复：文件名使用可读前缀 + 原 key SHA-256 摘要，并兼容迁移旧文件。

### 9.5 后台子任务在主循环关闭后回注

问题：关机先停止 AgentLoop，再等待 subagent 完成，会把 completion 写入无人消费的 inbound queue。

修复：运行时关机先取消后台子任务，再关闭 Loop；用户 cancel 与 shutdown cancel 使用不同语义。

### 9.6 SSRF 重定向绕过

问题：只校验初始 URL 时，公网地址可以 302 到 localhost 或私网。

修复：自定义 redirect 处理，每一跳重新解析 DNS/IP，并限制响应大小和内容类型。

### 9.7 插件初始化半成功

问题：插件先注册工具后 initialize 报错，会留下半加载工具。

修复：记录插件注册资源，失败时统一 rollback；坏插件错误隔离，不阻塞后续插件。

### 9.8 删除 Session 后长期记忆仍存在

问题：用户删除对话，但 consolidation 产生的记忆继续参与召回。

修复：Memory 记录 source_ref，Session delete callback 将对应记录标记 forgotten 并重写托管 Markdown。

### 9.9 后来又抓到的四个（按发现方式分类）

前八个都是"跑着跑着发现不对"。后面这四个的价值在于**发现方式各不相同**，
它们对应 [ENGINEERING_METHOD.md 第 4 节](./ENGINEERING_METHOD.md)的四种腐坏方式：

| Bug | 发现方式 | 类型 |
| --- | --- | --- |
| `compile_proactive_sources` 引用数为 0，"缺口已闭合"是假的 | **自查调用点** | 死代码 |
| 引擎 `closeables` 从未被遍历；`coremem.db` 被打开两次 | **DI 分层后追问"谁关它"** | 资源泄漏 |
| consolidation 移交时丢了"等上一轮收口"的 guard | **移交清单逐条勾对** | 移交丢行为 |
| `PostResponseMemoryWorker` 在真实模型下整条路抛 ValueError | **实弹（mock 抓不到）** | 契约偏差 |

最后一个值得展开：Reference 的 `_parse_json_string_array` 要求裸 JSON 数组，
deepseek 在同一段 prompt 下会返回 `{"intent": []}`——单键对象包着数组。
mock provider 永远按约定返回，所以 429 个测试全绿；而且这条路要配好 embedding
才会走到，双重掩盖。

修法有一条**边界纪律**：容错写在 kirakira 自己的 `coremem/compat_worker.py`，
镜像 Reference 的文件保持逐字节一致，doctor 漂移审计仍报 `drifted=[]`。
**在别人的代码里打补丁，会让你永远无法判断自己漂了多远。**

## 10. 上下文从“拼得出来”到“可治理”

### 10.1 旧实现为什么不够

原来的 ContextBuilder 能把 identity、memory、skills、history 和当前消息拼进请求，overflow 时再对
messages 做 microcompact。问题不是不能运行，而是**没有决策结构**：稳定提示和逐轮检索混在一起，
工具 schema/图片没有进入统一预算，裁剪后无法回答丢了什么，Provider 报错也只能盲目缩历史。

长上下文模型把这个问题藏得更深：1M window 下平时不爆，不代表工具解锁、图片或一次巨大 tool
result 后仍安全；等线上偶发时又没有 trace 可以复盘。

### 10.2 PromptBlock 与 Context Frame

迁移后的装配链是：

```text
PromptBlock(priority, label, static/cache signature)
  -> SystemPromptBuilder
  -> PromptAssembler
      stable system
      + history
      + dynamic Context Frame
      + current user
```

`identity/behavior_rules/skills_catalog/self_model/long_term_memory/session_context` 进入稳定 system；
`recent_context/active_skills/retrieved_memory/turn_injection/plugin_hints` 进入带系统标记的动态 frame。
Frame 明确声明“不是用户陈述”，解决了检索记忆、skill 指令和插件 hint 身份混淆的问题。稳定 block
按 workspace + 内容签名缓存，动态 block 每轮重建。

### 10.3 预算必须看到 Provider 真正看到的东西

统一输入预算：

```text
input_budget = floor(context_window × effective_context_percent) - max_tokens
estimate = system + messages + tool schemas + image allowance
```

Runtime 在 render 后和每个 ReAct step 估算，发出 `ContextPrepared`；OpenAI-compatible client 在
构造最终 payload 时再次预检。第二层尤其重要，因为 `tool_search` 可能在中途解锁新的 schema。
估算器是保守 chars/3，不是假装精确 tokenizer；Provider `usage` 会另外采集并作为实际值保存。

### 10.4 语义降级与历史安全线

错误重试不再修改上一次渲染结果，而是依次重新 render：

```text
full
→ drop skills_catalog
→ drop recent_context
→ drop long_term_memory
→ drop retrieved_memory
→ history 50%
→ history 0
```

每次历史切片回到 user boundary。正常历史从 `last_consolidated` 开始；未归档区达到安全阈值时，
下一轮先推进 consolidation，游标不前进则明确阻断，避免“为了能请求模型”而静默忘掉消息。
长 tool result 改为保留头尾，并标出总行数和省略字符数，末尾错误不再因只截头部而消失。

### 10.5 可观察性与在线验收

assistant message 的 `context_trace` 保存 attempt、disabled sections、history window、每个 block 的
chars/estimate/static/cache hit、ReAct request 数和模型 usage；session metadata 的
`context_budget` 保存下一轮 history baseline。TUI/Plain CLI 在执行中直接显示计划与预算。

真实 DeepSeek 冒烟结果：`full` 计划估算 3106/891808 input tokens，Provider 报告
3195 prompt + 15 completion = 3210 total，回复后 history baseline 22 tokens；完整 trace 成功落盘。

仍需正视一个边界：overflow 如果发生在有副作用的工具之后，外层 plan retry 会重新运行 Reasoner，
可能再次选择工具。因此外部工具仍需幂等；将来做跨 attempt tool-result replay 必须有专门评测，
不能为了省一次调用破坏上下文一致性。

## 11. 第七轮工程化：把 reference 追平，以及"照抄"和"照着做"的区别

前六轮里，我们和 reference 的关系一直是"读懂它的设计，用在自己的被动链路上"。这一轮把关系
说得更清楚了：**跟着 reference 走，不等于把 reference 的文件拷过来**。同一句"按 reference 来"，
在工具、记忆、skill 三条线上,分别逼出了三种不同的"照着做"。

判断标准始终是同一条，和第 8 节一脉相承：

> 一个能力只有当它背后的运行时真的存在、真的自洽时才算数。跑得起来不等于对，拷过来不等于
> 接上了。

### 11.1 工具追平：一个"跑得起来"的工具是怎么烂掉的

`web_search` 原来打 DuckDuckGo 的 HTML 端点。它写出来的那天是好的，测试也过，然后在没有任何
代码改动的情况下**悄悄坏掉了**——DDG 现在对这个端点返回反爬页面。没有报错、没有异常，只是
每次搜索都拿回一段"请证明你是人类"。这正是第 8.2 节那句话的翻版：**看起来正常、其实已经损坏**，
而且这次连崩溃都没有，纯靠人去用才发现"检索模块表现很差"。

修复不是去和反爬斗智，而是回到 reference：它用 Exa 的公开 MCP 端点（`https://mcp.exa.ai/mcp`，
JSON-RPC + SSE，无需 key）。我们照它的做法把 `web_search` 重写成打这个端点。这一轮顺带把另外
三个工具也对齐到 reference 的实际行为：

```text
web_search   DDG 反爬 HTML  →  Exa 公开 MCP（keyless，JSON-RPC over SSE）
web_fetch    只有纯文本      →  format=markdown（html2text）/ plain（lxml），保留 SSRF/大小/charset 治理
vision       朴素 base64     →  Pillow 编码（EXIF 转正、RGB 归一、4096 缩边、JPEG 质量回退），带数据 URI 预算上限
tool_search  英文分词        →  CJK bigram 归一 + 字段加权打分，中文查询也能命中
```

这一步的收获不在某个工具本身，而在于：**"这段代码当初能跑"不是它现在还对的证据**。外部依赖
会在你背后变。对齐 reference 的价值，一半是拿到更好的实现，另一半是拿到一个还在被维护、
不会悄悄烂掉的依赖。

### 11.2 长期记忆：全量搬一套子系统意味着搬它的"持久状态"

reference 的记忆早已从我们抄来的那版演进成独立的 `memory2`（结构化 SQLite store、memorizer、
retriever、embedder、post-response worker、consolidation）。这次的要求很直接：**整套搬过来，
然后做兼容**——不是挑几个函数,是把 `memory2/` 和 `coremem/` 整个 vendored 进来。

"做兼容"落在两处：

- 内部 import 全部改写到 `kirakira_agent.*` 命名空间。
- 外部依赖（httpx、原子写）路由到 `kirakira_agent/_compat/` 下的 shim（`net_http.py` 的
  `HttpRequester`/`RequestBudget`，`json_store.py` 的 `atomic_write_text`），逻辑保持原样。

但真正的教训不在搬代码，而在**搬状态**。`memory.py` 里把 `MemoryStore2`（`memory2.db`）接成了
记忆的持久后端：`memorize()` 两个返回点都 `_mirror_to_store2`，让新事实同时进托管 Markdown 和
store2。看起来接完了——直到线上出现一个很典型的 bug：

> 用户让 agent 删记忆，agent 回"已经全部清除干净了"，但 `memory2.db` 里那两条还是 active。

根因是 `forget()` 只改了托管区，没有传播到 store2。两个存储各说各话，而对用户可见的那句"清除
干净了"是基于其中一个的——另一个默默地把被删的事实继续参与召回。这是"半成品"最阴险的形态:
**主路径看着对，副本在背后漂移**。

修复分两层，且刻意都做上：

```text
forget()  →  _forget_in_store2(type, content)   把删除同步到 store2（标记 superseded）
启动时     →  _reconcile_store2_forgotten()       扫一遍 forgotten 记录，自愈历史漂移
```

第二层（启动自愈）是关键：它承认"两个存储可能已经不一致"这个现实，而不是假设"从此以后都会
同步"。**接一个持久后端，不只是让写入两边都落，还要让它能从过去的不一致里恢复。** 只做第一层，
历史上已经漂移的数据永远错下去。

### 11.3 Skills：一个 SKILL.md 只有当它的运行时存在时才算"能力"

最后一条线是把 `skills/` 对齐 reference。这里最先要纠正的是"对齐"这个词本身。

> 本节保留的是**被动链路阶段当时的判断**：当时主动/Drift runtime 尚未进入这条演进线，因此相关
> skill 被归入不搬。它们后来的 MVP 状态不在本文续写，见 [主动链路架构](./PROACTIVE_ARCHITECTURE.md)。

原来仓库里有三个 skill（`python-coding`、`repo-navigation`、`test-debugging`）——它们是**自研的
开发助手**，reference 里根本没有。reference 的九个 skill 是另一套：跑外部 CLI 的、管 MCP 的、
管插件的、调 Codex 的、以及几个依赖 akashic 独有子系统的。所以"对齐"不是改几行，是**换一整套，
并且判断哪些换得动**。

先把加载器补齐到 reference 语义（`kirakira_agent/skills.py`）：YAML frontmatter、
`metadata.akashic.requires` 的 bins/env 可用性门控、`when_to_use`。有了门控，`summarize` 这种
依赖外部 CLI 的 skill 在缺 `summarize` 命令时会**自动标记不可用、不进候选清单**，而不是让模型
选中一个跑不了的工具。为此加了 PyYAML 依赖。

然后按能力把九个 skill 分三层处理：

```text
Tier 1  能力已存在        weather / summarize（照抄）
                          skill-creater / manage-workspace-mcp（改工具名、路径）
Tier 2  补几个工具再搬      plugin-system  ← 补 plugin_enable/disable/uninstall 三个工具
                          codex-delegate ← shell→bash、run_in_background→mode、gate on codex bin
Tier 3  缺整套子系统       akashic-call / create-drift-skill / create-proactive-source  ← 不搬
```

Tier 2 的 `plugin-system` 逼出一个诚实的差异：**kirakira 的插件模型和 akashic 不一样**——
workspace 内安装、无 marketplace、无热重载、改动全部要重启。把 akashic 的 SKILL.md 照抄过来会
教模型去调不存在的命令、找不存在的路径。所以这个 skill 是照 kirakira 的真实模型重写的，同时
把缺的三个管理工具（`plugin_enable`/`plugin_disable`/`plugin_uninstall`，写 `.kirakira/manifest.toml`）
补上，让文档描述的能力真的存在。

Tier 3 是这一轮最重要的判断，也是第 8.2 节那条规则在 skill 上的直接应用。这三个 skill 都是
**薄薄一层文档，底下压着一整套 kirakira 没有的运行时**：`akashic-call` 要外部调用 gateway，
`create-drift-skill` 要 Drift 空闲运行时，`create-proactive-source` 要 proactive-sources 运行时。
只把 SKILL.md 拷过来，会得到三个**在清单里看着可用、选中后什么都做不了**的假 skill。

> 这正是"看起来正常、其实已经损坏"的 skill 版本。一个 skill 不是一份文档，是"文档 + 它假设
> 存在的运行时"。运行时不在，skill 就不算数。

所以它们被**明确推迟**：要搬，得先像 `memory2` 那样把对应运行时端口过来，每个都是一次独立的
子系统 port，而不是一次文件拷贝。宁可少三个 skill，也不要三个骗人的 skill。

### 11.4 这一轮的收获

```text
跑得起来 ≠ 还对          外部依赖会在背后烂掉，对齐 reference 也是在换一个还被维护的依赖
搬代码 ≠ 搬状态          接持久后端要让写入两边落，还要能从历史不一致里自愈
照抄 ≠ 照着做            reference 的模型和你的不一样时，重写文档去描述你的真实模型
文档 ≠ 能力              SKILL.md 背后的运行时不存在，这个 skill 就是假的，宁缺毋滥
```

三条线不同，判断准则是同一条：**能力以运行时为准，不以代码或文档为准。** 这和第 8 节"宁可全旧
不要半新"、"降级后自洽才能降级"是同一种洁癖——只是从 MCP、记忆写入，延伸到了工具、记忆删除
和 skill。

### 11.5 异步原生 model runtime：去掉"同步裹线程"的阻抗

追平 reference 到这一步，暴露出一个**架构层**而非行为层的差距:reference 的整条 runtime 是异步原生的
(`model_runtime` 层 + `await provider.chat`),而 kirakira 的模型客户端只有同步 `complete`(urllib +
`time.sleep`)，runtime 靠 `asyncio.to_thread(model_client.complete)` 在五六处裹成异步。

这套"同步内核裹线程"平时能跑，但一旦要接 reference 那些**异步原生的插件接口**(尤其 `engine.query`)，
seam 两侧异步性就错位——同步的 `to_thread(memory.retrieve)` 对上异步的 `await engine.query`，处处别扭。

修法是把客户端做成异步原生:

```text
OpenAICompatibleClient
  + acomplete / acomplete_stream(httpx.AsyncClient，SSE 解析与同步版共用一份)
runtime / _compat.provider
  优先 await 异步；只有同步 stub 客户端才回退 to_thread(complete)
```

收益是**根上**的:memory / proactive / drift 的所有 LLM 调用(都走 `_compat.provider`)现在都是异步原生，
不再裹线程。记忆接线感到的那种"外来阻抗"从此消失。老式同步 stub 靠"优先异步、回退同步"零改动继续绿。

> 这是 kirakira 与 reference 的四个**地基级重构**中的第一个(异步 runtime / 依赖注入 / Turn+lifecycle
> DAG / 记忆 seam)。"接个东西搞半天"的感觉，本质是在没打好的地基上硬插;先把地基对齐，上层就顺了。

### 11.6 记忆:从"兼容 façade"到 `DefaultMemoryEngine`

reference 那句"一行就接好"的 `engine.query()`,底下垫着一个把所有零件装配好的引擎。kirakira 之前把零件
(`memory2` 的 retriever / memorizer / hyde / …)都复制来了，却**没有装配者**——被动 turn 只能调旧
`MemoryRuntime.retrieve` 的同步词法，引擎语义(intent 分流、HyDE、改写、语义去重、自动摄入)全缺。这就是
"零件都在、却停在 M1"的真正原因。

这一轮把装配者补上:`memory2` 整体折叠进 `coremem` 单包(数据库随之 `memory2.db → coremem.db`)，照抄
reference 的 `DefaultMemoryEngine`;被动 turn 改调 `engine.query(MemoryQuery(intent="context"))`,检索智能
搬到引擎后面，runtime 不再感知。承重需配 `[memory.embedding]`(reference 用 DashScope text-embedding-v3)。
详见 [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md)。

### 11.7 依赖注入:`MemoryServices` 缝

光有引擎还不够——如果 runtime 直接 `import` 具体引擎类，换实现就要连锁改一堆调用点和测试。reference 的解法
是 `MemoryServices(engine)`:runtime 只认识这个薄服务包,不认识实现。

```text
build_memory_services(...)  → 门控:配了 embedding → DefaultMemoryEngine，否则 DisabledMemoryEngine
PassiveTurnPipeline(memory_services=…)  → 检索走 memory_services.engine.query(...)
```

这是四个地基重构里"依赖注入"的第一次落地。它让"换记忆实现只换一个 service 包"成立，也是后续把
context / session 也服务化的模板。

### 11.8 这两轮补充的收获

```text
同步内核裹线程 ≠ 异步原生      seam 两侧异步性一致，插件接口才不别扭；地基不对，上层处处费劲
零件齐 ≠ 子系统成            复制来算法零件不等于能跑，缺的是把它们装配成一条接口的引擎
import 具体类 ≠ 可替换        依赖注入让 runtime 只认接口；换实现不连锁改，才谈得上"照 reference 演进"
```

## 12. 第八轮：把四个地基一次做完

前七轮是"遇到问题补一层"。到第七轮末尾出现了一个新症状:**补不动了**——
接记忆引擎要绕线程,单测一个模块要构造整个 runtime,主动链路想插一步做不到。
这说明剩下的问题不再是"少了什么功能",而是"抽象的形状不对"。

于是这一轮不加任何功能,只做四件事,并且**按依赖顺序做完才碰上层**。
判断这四件事为什么是"地基"而不是"加法",见
[ENGINEERING_METHOD.md 第 3 节](./ENGINEERING_METHOD.md)。

### 12.1 地基①:异步原生 model runtime

**之前**:客户端只有同步 `complete`(urllib + `time.sleep`),runtime 里满地
`asyncio.to_thread(model_client.complete)`。

**问题不在性能,在缝**。记忆引擎、主动判断、Drift 的 LLM 调用全是 async 接口,
每接一个都要在边界裹一层线程。裹线程本身能跑,但它让每一个 seam 都变得别扭,
而"别扭"会诱导人再加一层适配——债务就是这样滚起来的。

**做法**:客户端补 `acomplete` / `acomplete_stream`(httpx),runtime 与
`_compat.provider` **优先 await 异步、只有同步 stub 才回退线程**。
SSE 解析在同步/异步两条流之间共用一份实现,保证行为一致。

**收获**:这一改之后,记忆接线时那种"处处费劲"的感觉直接消失了。
症状出现在记忆,病根在模型运行时——这是本项目最典型的一次"别在症状处打补丁"。

### 12.2 地基②:依赖注入(Services / Ports)

**关键设计是把两类东西分开**:

```python
LLMConfig / MemoryConfig        # 值:可序列化、可比较、进得了配置文件
LLMServices / SessionServices   # 对象:有生命周期、要关闭、不可序列化
ContextServices / MemoryServices
```

混在一个 dict 里传是最常见的退化形式。一旦混了,你就回答不了
"**关停时该释放什么**"——本项目正因为分开了,才发现引擎的 `closeables`
从来没被遍历过(一个真实的资源泄漏)。

**验收标准**是可操作的:单测一个模块时,不需要构造整个 runtime。

### 12.3 地基③:Turn 抽象 + lifecycle slot DAG

分两部分。

**Turn 结果与副作用**:一轮的产出不再是"随手改几个地方",而是一个
`TurnResult`,副作用分三类显式声明:

```python
side_effects           # 无论成败都执行(发送前的状态落地)
success_side_effects   # 仅在出站成功后执行
failure_side_effects   # 仅在出站失败后执行
```

这个三分不是设计洁癖。主动推送原本在 alert / content / drift 三条路径上
各手写一遍"发送→成功写 session→失败回滚",三处的回滚逻辑还不完全一致。
收敛到 `commit_turn_result()` 单点后,跨崩溃去重才有唯一的挂载位置。

**slot 依赖图**:模块声明 `slot` / `requires` / `produces`,拓扑排序决定顺序;
依赖缺失的模块**级联禁用**而不是静默乱序。内置 slot 豁免。

这里有一个刻意的克制:Reference 的 `PhaseFrame` **没有移植**。因为我们的相位
模块签名还是 ctx 对象,引入 frame 却没有模块用 `frame.slots` 传递产物,
它就会是 §4.1 说的那种"声明了但没通电"的代码。缺口写在
[NOW.md](./NOW.md) 第 3 项,而不是先摆一个空壳。

### 12.4 地基④:记忆 seam

**之前**:`memory2` 的算法零件全部搬进来了——retriever、memorizer、HyDE、
embedder、rule schema……**但没有装配者**。被动 turn 调的还是旧的
`MemoryRuntime.retrieve`(同步词法)。

这就是"零件都在,却还停在 M1"的根因:**缺的是把零件装起来的 engine**。

**做法**:照抄 Reference 的 `DefaultMemoryEngine`(约 1520 行),
runtime 只看到一条缝:

```text
PassiveTurnPipeline
  → memory_services.engine.query(MemoryQuery(intent="context", scope=…))
  → 引擎内部:HyDE / 改写 / 多路召回 / RRF / 注入预算(runtime 完全不感知)
  → text_block 注入上下文
```

**门控**:配了 `[memory.embedding]` → 引擎承重;没配 → `DisabledMemoryEngine`,
回退旧词法路径(因为引擎读写都要向量)。

**这一轮学到最贵的一课**:搬一个子系统,意味着搬它的**持久状态和恢复合同**,
不只是算法。记忆迁移分 M0→M1→M2→Stage 3/4/5 六步,其中算法反而是最容易的,
难的是"唯一 owner、迁移、回滚、资源释放"。

### 12.5 这一轮的收获

```text
地基有依赖顺序          ①异步 → ②DI → ③Turn/slot → ④记忆 seam,顺序错了会返工
不做的比做的更重要      PhaseFrame 明确不移植,因为没有消费者;空壳比缺口更有害
症状处 ≠ 病根处          记忆难接,病根在模型运行时
搬子系统 = 搬持久状态    算法最容易,恢复合同最难
```

## 13. 第九轮：可扩展与可运维

四个地基就位之后,上层"加法"才第一次变得便宜。这一轮做的都是坐在地基上的东西。

### 13.1 插件扩展体系:从"能加工具"到"能热换代"

原本插件只能注册工具和 hook。这一轮照 Reference 补齐成完整体系:

- **声明式规格**:`plugin.py` 程序化声明能力(工具、MCP server、托管服务、
  作业、主动源、相位模块),`manifest.toml` 只管启停;
- **per-plugin 代际 + 租约**:在途 turn 持有当前代际的租约,
  热重载**不会抽走 turn 正在用的工具**;退役代际要等租约归零才能排空;
- **热重载与安装免重启**:watcher 唤醒 → reconcile 增删 → 原子替换升级;
- **gate**:语义检查不过的插件**不发布代际**,而不是半启动。

代际租约是这一轮最重要的一课:**"边跑边改"的系统里,唯一安全的模型是
"在途请求看到的是它开始时那一代"**。没有这条,热重载就是在给自己埋竞态。

### 13.2 主动链路 lifecycle 化

主动 tick 从一个扁平大函数,改成模块流水线:

```text
gate → fetch → ingest → judge_context → alert → content → drift
```

顺序由各模块的 `requires` 依赖图决定,**插件可以声明依赖后插进中间任意位置**。
Drift 从"主循环末尾的一个 if 分支"变成流水线上的一个模块 `proactive.drift`。

同期把 Drift 的触发从固定 `min_interval` 改成 **hazard 采样到期**:
开销驱动 × 内容/近期/重复抑制,解 `∫rate dt = -ln(1-u)` 得到下次到期时刻。
这样做的理由写在 [decisions/0005](./decisions/0005-drift-hazard-sampled-expiry.md):
轮询判阈会产生"查得越勤触发越频繁"的伪影,采样到期没有这个问题。

### 13.3 跨崩溃投递去重

主动消息在"渠道发送成功"和"本地状态提交"之间崩溃,重启后会重复发送。

选的方案是 **内容 sha256 + 时间窗**(照 Reference 的 `deliveries` 表),
**明确不做两阶段 outbox**。代价写在
[decisions/0004](./decisions/0004-delivery-dedup.md):
语义是"**至多一次 + 窗口内不重复**",不是 exactly-once——标记后、发送前崩溃
会漏发这一条。把代价写清楚,比含糊地说"已解决可靠性"有用。

### 13.4 控制面:从"能跑"到"能运维"

在此之前,一个跑着的 agent 只能通过渠道对话或看日志。**没有任何程序化入口**
可以问"你在忙什么"、"把这轮掐掉"、"跑一轮但别发到群里"——一切干预都要重启进程。

控制面提供这条缝:workspace 私有 Unix socket 上的 JSON-RPC 2.0 over NDJSON,
与渠道链路完全并行。分层、turn 状态机(SQLite CAS)、慢消费者隔离、认证细节见
[design/control-plane.md](./design/control-plane.md)。

两个值得记的设计:

- **`ConversationRuntime` 不知道 agent 怎么跑**,它只拿一个 `TurnExecutor`。
  正因为如此,控制面能在不改被动链路一行的前提下接上去。
- **慢消费者只毒死自己**:订阅队列满时不阻塞发布方,而是清空该队列、
  塞进 `SlowConsumerError` 并把它踢出订阅集。一个卡住的客户端不能拖慢 turn。

同轮补上 `request_user_confirmation`。这里有一个**认知修正**值得记录:
我一开始以为它是"危险工具的执行前闸门",读了 Reference 实现才发现
**它是标记,不是闸门**——它不阻止任何工具执行,只把 `mobile_attention` 抬到
turn 级供渠道渲染。真正的拦截原语是 `tool_hooks` 的 pre-hook `deny`,
而那个我们本来就有。

> 教训:**读实现,不要从名字推断语义**。"confirmation" 这个词让我先入为主地
> 以为它是闸门,如果不是去读了那 30 行代码,就会把一个错误结论写进文档。

### 13.5 这一轮的收获

```text
地基就位后加法才便宜     控制面接上去没改被动链路一行
边跑边改必须有代际        在途请求看到它开始时那一代,否则热重载=埋竞态
把代价写清楚              "至多一次+窗口内不重复" 比 "已解决可靠性" 有用
读实现,别从名字推断      request_user_confirmation 差点被我写成闸门
```

## 14. 下一版：工具编排 + LangSmith 评测回归

下一版最重要的不是继续加普通工具，而是证明“工具选择更准、参数更稳、改动不会让旧场景退化”。

### 14.1 Trace 接入

将以下节点记录到 LangSmith 或兼容 trace runner：

- turn/session/channel/model。
- PromptRender 后的输入摘要和可见工具名。
- 每次模型返回的 tool selection。
- schema validation、pre hook、handler、post hook。
- tool latency、error、retry、denied reason。
- memory query、候选、排名和最终注入。
- final answer、token、latency 和终止原因。

敏感字段必须脱敏，API key、完整私密附件和高风险工具参数不能原样上传。

### 14.2 工具系统评测集

至少覆盖：

- 正确选工具。
- 不需要工具时不调用。
- 相似工具消歧。
- 缺参数、错类型、错 enum。
- deferred tool 是否先 search 再调用。
- MCP 工具断连与 fallback。
- 重复工具调用是否及时停止。
- 高风险工具是否被 Hook 拦截。
- 工具成功后最终回复是否忠于结果。

### 14.3 记忆评测集

- 应记住的稳定偏好是否写入。
- 短期状态是否不会被误存为长期事实。
- 历史事实与当前事实冲突时是否正确处理。
- 删除 session 后 source memory 是否失效。
- 口语改写是否能语义召回。
- 变量名、路径、错误码是否能关键词召回。
- 无关问题是否不会注入噪声记忆。
- consolidation 重放是否幂等。

### 14.4 基线、回归与回滚

```text
固定 Dataset
  -> baseline commit + prompt/model/config
  -> candidate commit
  -> deterministic/code evaluators
  -> LLM judge（只用于难以规则判断的质量项）
  -> 对比准确率、工具成功率、记忆命中、token、p95 latency
  -> 未过阈值则阻止发布
```

每次运行记录 commit SHA、模型、Prompt 版本、工具 schema 版本、memory strategy 和 evaluator 版本。回滚不是“凭感觉改回 Prompt”，而是回到最后一个通过 gate 的版本和配置。

### 14.5 下一版验收指标

- 工具选择准确率。
- 工具参数一次通过率。
- Tool Loop 完成率和平均迭代数。
- 重复调用拦截率。
- 记忆写入 precision、召回 recall、无关注入率。
- token、TTFT、turn p50/p95。
- Bug fixture 全量回归通过。

具体阈值应在第一批真实数据跑完后确定，不能在没有 baseline 时随意编百分比。

## 15. 再下一版：100–200 用户的后端化

这一层目前是设计方向，不应写进当前简历的“已完成”部分。

### 15.1 服务拆分建议

```text
FastAPI / WebSocket / SSE Gateway
        ↓
Auth + Rate Limit + Moderation
        ↓
PostgreSQL：user/session/message/turn/memory/job
        ↓
Redis：短期状态、分布式锁、队列/stream、限流
        ↓
Worker：Agent Turn / Memory Consolidation / Embedding
        ↓
LLM、Tool/MCP、pgvector、对象存储
```

### 15.2 多用户隔离

- 所有业务表带 `tenant_id/user_id`。
- Repository 查询默认注入用户作用域。
- Session lock 从进程内 Lock 升级为数据库或 Redis 锁。
- Worker 消费使用幂等 turn id。
- PostgreSQL 可增加 Row Level Security 作为第二层隔离。
- Tool workspace、插件数据和附件路径按用户隔离。

### 15.3 数据模型

- `users`：身份、状态、配额。
- `sessions`：channel、owner、metadata、version。
- `messages`：role、content、media、seq。
- `turns`：pending/processing/done/failed、trace、token、error。
- `tool_calls`：name、arguments、status、latency、result reference。
- `memories`：type、summary、embedding、source、status、confidence。
- `jobs`：schedule/subagent/consolidation 的统一状态机。

### 15.4 Worker 与并发

- API 只负责提交 turn 和返回 turn id。
- Worker 异步执行 AgentLoop。
- `FOR UPDATE SKIP LOCKED` 或消息队列避免重复消费。
- 同 session 使用 advisory lock/Redis lock 保序。
- 结果通过 SSE/WebSocket 或轮询回传。
- Tool call 和 memory write 使用幂等 key。

### 15.5 内容审查

- 入站文本、附件和出站回复分别审查。
- 高风险工具调用走 policy engine，而不是只审查最终文本。
- 审查结果、规则版本和处置写入 audit log。
- 对误杀提供人工复核和申诉状态。
- 管理后台展示用户、turn、tool call、memory、moderation 和 trace。

### 15.6 什么时候可以写进简历

至少完成：

- PostgreSQL 多租户数据模型与 migration。
- API/Worker 分离和同 session 并发控制。
- 100–200 虚拟用户压测报告。
- 内容审查链路与后台查询。
- 故障恢复和幂等测试。

完成后再把简历前两条升级为“FastAPI + PostgreSQL + Worker”，否则面试追问很容易露出没有真正实现。

## 16. 被动链路如何讲

项目主线应是：

1. 先做最小 Function Calling 闭环。
2. 发现工具参数、异常、超时和上下文问题，抽象 ToolRegistry/ToolExecutor。
3. 工具变多后加入 deferred discovery、MCP 和 Hook。
4. 长对话引入 Session、历史搜索、结构化长期记忆和异步 consolidation。
5. 多入口引入 MessageBus、同 session 串行和跨 session 并发。
6. 通过真实 Bug 补齐 correlation、reasoning 回放、SSRF、rollback 和 graceful shutdown。
7. 把 Prompt 拆成具名 block，用 Provider 预检、语义降级和 trace 管理长上下文。
8. 追平 reference：对齐工具行为、迁移 memory2 核心状态与行为并接入持久后端、按运行时能力分层搬 skill——
   在这一轮里把"能力以运行时为准"从架构原则贯彻到工具、记忆删除和 skill。
9. 下一版用 LangSmith/eval 把经验固化为可回归的工程指标。

这条演进链只回答“被动 Agent Runtime 是怎样工程化的”。项目的产品差异化、主动触发与 Drift
不要硬塞进这条叙事，单独按 [主动链路架构](./PROACTIVE_ARCHITECTURE.md) 来讲。
