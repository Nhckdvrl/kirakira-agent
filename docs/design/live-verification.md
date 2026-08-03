# 实弹验证记录:哪些链路真的跑过

- 状态:accepted;第 2–13 节是历史实弹记录，第 14 节是 2026-08-04 架构迁移后的 DeepSeek/embedding 重验
- 当时核对的上游基线:`012e37c8b51df045353972bb551d8e868ab52455`（历史证据，不是运行时依赖）
- 目标读者:维护者、评审者、接手做下一轮验证的人
- 关联:[NOW.md](../NOW.md)、[decisions/0004](../decisions/0004-delivery-dedup.md)

标注:**F** 已实际执行并观察到结果;**G** 尚未验证。

## 1. 为什么单独记这一份

离线回归（2026-08-04 为 584 passed + 4 subtests）只说明**单测口径**下的行为成立。它抓不到两类问题:

1. **跨组件的真实链路**——测试用 mock provider 与替身渠道,组件之间的真实交互没被走过;
2. **真实模型的输出偏差**——mock 永远按约定返回,真实模型不会。

这份文件区分"测过"和"跑过",避免把前者说成后者。一次真实验证就抓出了下面第 6 节那个 bug。

## 2. 模型与传输(F)

| 项 | 结果 |
| --- | --- |
| `acomplete` 非流式 | 真实端点返回,usage 正常 |
| `acomplete_stream` 流式 | 9 个增量块,内容完整 |

此前只有单测覆盖(同步 stub 走回退路径),异步原生路径从未对真实端点发过请求。

## 3. 被动链路与记忆(F)

| 项 | 结果 |
| --- | --- |
| 一次完整对话 | 2.7s,context 预算 2960/111104,success |
| `memorize` 写入 | 经引擎落库为 `preference`,**向量确实入库**(1024 维,`vec_items` 表) |
| **跨 session 检索** | session A 写入"部署脚本在 scripts/rollout.sh";**新 session、0 条历史**下提问,回复以"根据记忆"开头并给出该路径 |
| `recall_memory` 工具 | 走引擎,success |

跨 session 那条是记忆子系统最核心的验证:历史里没有该事实,只能来自长期记忆检索。

## 4. 主动链路(F)

隔离 workspace + 本地捕获渠道,**真实模型判断**:

| 项 | 结果 |
| --- | --- |
| 模块流水线顺序 | gate → fetch → ingest → judge_context → alert → content → drift |
| alert 自然化 | 原文"API 错误率升到 12%"→ 模型输出"喂,生产环境出问题了!API 错误率已经飙到 12% 啦" |
| 投递 | 1 条 |
| **第二次 tick** | **发出 0 条**,决策 `idle`——内容指纹去重生效 |

真实 Telegram 投递(用户授权后执行):决策记录 `alert_pushed`,消息送达。

同一轮顺带覆盖了 `ProactiveJudge` 的严格 JSON 与 `finish_drift` 参数解析,未出现格式偏差。

## 5. Drift 与插件(F)

| 项 | 结果 |
| --- | --- |
| Drift 一轮 run | `explore-curiosity` 完成,`message_result=sent` |
| 消息 | "最近有没有在听什么歌或者播客?想找点新的听听" |
| **`journal_append`** | **被模型真实调用并落库**,内容是它对本轮的判断记录 |
| 插件 install | watcher 唤醒 → **免重启激活**,代际 `9c54ca2e03f3cb89` |
| 插件声明的主动源 | 被收集进 `proactive_sources` |
| 插件 upgrade | 换代到 `94ae7c21d9afce2d` |
| 插件 uninstall | 下线,当前代际清空 |

## 6. 这次验证抓到的 bug(F)

`PostResponseMemoryWorker` 的自动失效检测**整条路抛 ValueError**。

- 原因:Reference 的 `_parse_json_string_array` 要求裸 JSON 数组,deepseek 在同一段 prompt 下
  会返回 `{"intent": []}`——单键对象包着数组;
- 为什么单测没抓到:mock provider 不会这样返回,且这条路依赖引擎承重(配好 embedding)才会走到;
- 修法:`plugins/default_memory/compat_worker.py` 在 Kirakira 边界容错；当时的开发审计确认算法移植未漂移；
- 容错只放宽"单键对象且值是数组"一种,其余坏响应仍报错。

## 7. 控制面与确认工具(F,2026-07-26)

对**真实运行中的 agent**(`main.py gateway`,真实 deepseek)通过
`main.py control` 驱动,见 [control-plane.md](./control-plane.md)。

| 项 | 结果 |
| --- | --- |
| socket 建立 | `.kirakira/control.sock`,权限 `srw-------`(0600) |
| `server/status` | `ready=true`,workspace 正确 |
| 新建 thread + 一轮问答 | `programmatic:e8dbe5fb…`,真实模型回复 JSON-RPC 定义 |
| 带工具的一轮 | `toolCall: list_dir -> success` 落进 turn record |
| **中断在途 turn** | 6s 后 interrupt → `status=interrupted`,`completedAt` 已写;**重读一致** |
| 中断后 thread 复用 | 同 thread 新起一轮 → `completed`,回复"恢复正常" |
| 同 thread 并发 | 拒绝,`-32011` 且 `retryable=true` |
| 不同 thread 并行 | 正常完成 |
| 未知 thread | `-32010` |
| `thread/consolidate/start` | 返回 operation handle,异步执行 |
| 关停 | socket 文件被清理,无残留 |

**`request_user_confirmation`**:模型调用后,turn 的 assistant item 带
`mobileAttention: confirmation`;普通轮次不带。

### 这一轮抓到的问题

`outbound.metadata` 从来没有携带过 `tools_used` / `tool_chain`——Reference 的
`after_reasoning` 阶段会写,我们的没有。后果是控制面的 toolCall item 投影是**死代码**:
写了,但永远拿不到数据。第一次实弹跑就暴露了(turn 里只有 assistantMessage,没有
toolCall)。已按 Reference 补齐 `outbound_metadata`,重跑后 toolCall 正常出现。

同一类错误第二次出现(上一次是 `compile_proactive_sources` 没有调用点):
**写了投影逻辑但没验证上游真的产出数据**。单测里我用的是自己构造的 metadata,
所以测试是绿的——只有实弹能发现上游根本不填这个字段。

## 8. 仍未验证(G)

| 项 | 缺什么 |
| --- | --- |
| 主动 tick 租约在真实热重载下 | 双租约有单测,tick 中途真实插件升级未观察过 |
| scheduler 重启 misfire 恢复 | 恢复逻辑有单测,没做过"离线一天再启动"的真实验证 |
| 插件声明的 MCP 主动源端到端 | 只用替身 gateway 验过编译与注册,没有真实 MCP server 的 fetch/ack |
| Web / QQ / 官方 QQBot 渠道 | 只有 Telegram 做过真实投递 |
| 热重载与在途 turn 的竞争 | 代际租约有单测,但没在真实并发下观察过 |
| 跨崩溃去重的真实崩溃场景 | 用重开库模拟过,没有真正 kill -9 之后重启验证 |
| 长时间运行 | 没有连续跑数小时观察内存、连接与调度漂移 |
| 控制面 TCP 模式与 token 认证 | 只验过 Unix socket 无 token 路径 |
| `plugin/disable-and-drain` 真实排空 | 当前 workspace 没装插件,只验过错误路径 |
| 换代期间另一连接的 retryable 拒绝 | 第 9 节换代成功,但没有在冻结窗口内并发第二个连接观察 `-32011` |

## 9. 换代与 tool_choice(F,2026-07-26)

隔离 workspace(`[proactive].enabled=false`)+ 真实 supervisor + 真实 deepseek-v4-flash。

### agent_restart 真实换代

| 步骤 | 观察结果 |
| --- | --- |
| supervisor 拉起第一代 | bootId `a686cb79…`,readiness 1s 内就绪;控制面 `server/status` 回报同一 bootId(接线正确) |
| 控制面 turn 驱动 | 模型按指令 **自己 `tool_search` 发现并调用 `agent_restart`**(deferred 边界成立),工具返回 `{"status":"scheduled","requestId":"restart_3a8ba18…"}` |
| 提交与换代 | 最终回复送达 CLI 后,gateway 以 75 退出;supervisor 校验通过并**拉起第二代**(bootId `64189fd0…`,新 pid),前后约 1s |
| 第二代服务 | 控制面重新上线报新 bootId;新起一轮 turn,真实模型正常回复 |
| 关停 | SIGTERM supervisor → 转发 → 全部退出;control.sock 与 `.runtime-ready.json` 被清理 |

若提交帧无效,supervisor 会以 70(SUPERVISOR_FAILURE)退出而不是换代——它继续监管并拉起了第二代,
证明 `_valid_commit`(nonce/boot_id/单帧)真实通过。

两个顺带发现:

1. **AF_UNIX 路径上限(macOS 104 字节)**:深层 workspace 下默认 `control.sock` 绑定失败,
   控制面按设计被吸收(runtime 继续跑);`KIRAKIRA_CONTROL_ENDPOINT` 覆盖短路径后正常。
   部署到深路径 workspace 时必须用该环境变量。
2. **渠道随 config 启动,与 CLI 旗标无关**:`_build_channel_host` 直接读
   `channels.*.enabled`(telegram 还默认"有 token 即启"),隔离测试只关 proactive 不够,
   web+telegram 仍在测试进程内启动(~2 分钟窗口,无消息发出)。想完全隔离渠道,要在
   隔离配置里把 `channels.*.enabled` 也置 false。

### tool_choice 顺从度(deepseek-v4-flash,客户端直连)

| case | 结果 |
| --- | --- |
| `auto`(对照) | 闲聊 prompt 自然返回纯文本,无工具调用 |
| `required` | 同一 prompt 被服务端强制 → 产出 `journal_append` 调用,参数合理,`finish_reason=tool_calls` |
| 具名强制 `finish_drift` | 恰好调用 `finish_drift`,参数完整且 enum 合法(`status="paused"` + briefing) |

未出现 Reference `DeepSeekStrategy` 提示的 thinking 冲突(当前配置下)。换 provider 或开
thinking 后需重验,见 [NOW.md](../NOW.md)。

## 10. Web 仪表盘(F,2026-07-26)

真实 gateway(`main.py gateway`,真实 deepseek + 承重记忆引擎)+ 浏览器实访。

| 面板 | 结果 |
| --- | --- |
| 总览 | 引擎 `default · 承重`、5 条记忆、12 个会话、主动运行中、Drift 3 轮、`未托管 → agent_restart 不可用` 均正确 |
| 记忆 | 走引擎 admin 协议;能力集 8 项与工具面 `recall_memory/memorize/forget_memory` 由 `tool_profile()` 渲染;5 条记忆含 active/superseded 分状态 |
| 会话 | 12 个会话列表;点开 `telegram:1862986856` 显示 58 条、已归档至第 37 条与真实历史 |
| 插件与代际 | 空 workspace 下正确显示"暂无数据"(未装插件) |
| 主动与 Drift | 流水线 7 个 slot、电量 0.26/base 0.95、真实决策轨迹;Drift 3 轮运行含 sent/silent 与跨轮 scratchpad/倾向 |
| 聊天页 | 真实模型一轮问答正常渲染 |

### 这一轮抓到的问题

1. **状态库的线程亲和(真 bug)**:`proactive.db` / `drift.db` 的连接归事件循环线程独占,
   而 Web 的 HTTP handler 跑在 `ThreadingHTTPServer` 自己的线程里——首次打开面板直接报
   `SQLite objects created in a thread can only be used in that same thread`。
   修法是把读 marshal 回属主线程(`DashboardService._read`,同 `_next_event_sync` 的既有惯例),
   **而不是另开一条连接**——后者会破坏"状态库单一 owner"这条不变量。
   单测抓不到:测试里 DashboardService 与 store 在同一线程。
2. **空表头渲染成 `[object Object]`**:`esc(h.label || h)` 对空串会 falsy 回退到对象本身。
   改成 `h.label === undefined ? h : h.label`。

第 1 条与前两轮同属一类:**只有真跑才暴露的跨组件真实形状**(线程归属、上游字段、环境上限)。

## 11. 检索回放与批量删除(F,2026-07-26)

真实 gateway + 真实 deepseek,跑一轮"用 recall_memory 查我的身份设定"。

| 项 | 结果 |
| --- | --- |
| 两类记录写入 | `context_prepare` 与 `recall_memory` 各一条,**归到同一个 turn_id** `dfa4187e…` |
| 面板聚合 | 回放列表显示"自动召回 1 / 未注入 / 主动查询 1" |
| 详情 | 命中项含类型、内容、分数 0.515、注入标记;模型主动查询的参数与结果一并展示 |
| 消息检索 | 跨会话搜索"月火"命中真实历史,并标注只读原因 |
| 记忆批量删除 | 勾选/全选/按钮门控正确;**缺 confirm 令牌服务端返回 400**(`hard delete requires confirm=HARD_DELETE`) |

### 面板第一次使用就抓到一个检索质量问题

这轮的真实数据是:**自动检索召回了正确的 identity 记忆(分数 0.515),但 `injected=False`
——没进上下文**;模型随后主动调 `recall_memory(intent="answer")` 返回 0 条;于是模型回答
"没有找到关于你身份设定的记录"。

没有这个面板,现象只有最后那句"没找到",只能猜是哪一环出的问题。有了它可以直接读出:
召回没问题,是**注入阈值与 answer intent 的阈值**把它挡掉了。这条留作调阈值时的依据,
本轮不改——改检索阈值属于行为变更,应当单独评估。

## 12. akasha RAR 引擎(F,2026-07-27)

隔离 workspace(关渠道与主动)+ `[memory].plugin = "akasha"` + 真实 deepseek。

| 项 | 结果 |
| --- | --- |
| 引擎路由 | 仪表盘 `engine-info` 报 `akasha`,`load_bearing=true` |
| 工具面 | `recall_memory` + **`reinforce_memory`**(engine 自定义工具槽真的生效) |
| 第一轮 | 告知"部署脚本在 scripts/rollout.sh",正常回复;检索回放记录召回 0(库空) |
| **跨 session 检索** | **新 session、零历史**问"我发版要跑哪个脚本",答出 `scripts/rollout.sh` |
| 回放证据 | 引擎=akasha,召回 1 条,注入 True,score **0.7593**,命中项类型是 `turn` |
| 镜像保真 | `memory doctor` 报 akasha `checked=12, drifted=[]` |

命中项类型是 `turn` 而不是 `item`——这正是 akasha 与默认引擎的语义差:它把**整轮对话**
存成图节点,靠涟漪激活召回,而不是抽取成条目再做向量检索。

### 这一轮抓到的四个问题

1. **`timestamp` 从"不承重"变成"承重"(预测命中)**:审计里写过"Reference 传了
   `timestamp`,但 DefaultMemoryEngine 全文不读,**只有换 engine 才会承重**"。换到 akasha
   后第一次 query 就返回 `missing_query_timestamp`。三处 `MemoryQuery` 构造点已补。
2. **工具面不能有回退**:此前"profile 没声明就退回旧 schema 注册",导致 akasha 下模型
   看得到 `memorize`、调用后被引擎拒绝写入(akasha 从 turn 自动摄入,本就没有 memorize)。
   改为**声明什么注册什么**,并支持 `profile.tools` 自定义工具槽。
3. **真相源错配**:akasha 的 `DESCRIPTOR.notes` 写着 `truth=sessions.db/messages`,而
   kirakira 的会话是 per-session JSON。解法是在派生索引库里加一张 Reference 同形的
   `messages` 投影表 + 外部内容 `messages_fts`(三个触发器),并把索引库路径对齐到
   `workspace/sessions.db`。当时 JSON 仍被视为 canonical；当前状态已改为 SQLite 权威，见第 14 节勘误。
4. **外部内容 FTS 的 malformed 陷阱**:老库里 `messages` 先于 `messages_fts` 存在时,
   `DELETE FROM messages` 会触发删除不存在的索引项,SQLite 直接报
   `database disk image is malformed`(`integrity_check` 却是 ok)。派生物应当能无条件
   重置,故改为"先拆触发器与索引、清表、再原样建回"。

## 13. 设计意图逐项在线检验(F,2026-07-27)

akasha 与仪表盘一轮改动之后,用真实 gateway(默认引擎 + 真实 deepseek)逐项核对
**"实现是否满足当初的设计意图"**,而不只是"能不能跑"。

| 设计项 | 检验结果 |
| --- | --- |
| 默认引擎在 akasha 改动后仍承重 | `engine-info` 报 `default`、`load_bearing=true`、工具面三件套齐 |
| 引擎路由 fail loud | 配错名字 → `未知记忆引擎: 'akashaa'(可选 default / akasha)` |
| 批量删除的 confirm 保护 | 缺令牌 → 400 `hard delete requires confirm=HARD_DELETE` |
| 消息面板只读契约 | 命中 5 条,`deletable=false` 且带原因 |
| `compact` 真归档 | 归档游标 0 → **2**,回执"已归档 2 条历史消息" |
| subagent 禁用名单用真实工具名 | 14 项全部是已注册名,`mcp_apply` 在内 |
| 检索回放记录默认引擎路径 | 记录到 `engine=default` 的 context_prepare |
| 插件面板代际可观测 | 空 workspace 正确返回空集合 |

### 抓到一个真缺陷:非规范类型的记忆永远不会被注入

两次采样都是同一现象:检索**命中了正确的记忆**(score 0.515 / 0.5818,均高于
阈值 0.45),但 `injected=False`,模型于是答出与记忆不符的内容
(库里是"月火,傲娇",模型答"Kirakira,认真活泼")。

根因不在阈值,在**类型**:`retriever._select_injection_sections` 只接受
`procedure/preference` 与 `event/profile` 四类,其余落进 `else: continue`。
而这两条记忆的类型是 `identity` —— kirakira **旧工具 schema** 才有的类型
(Reference 的 enum 只有 event/profile/preference/procedure,靠 schema 就防住了)。
引擎的 `_coerce_memory_type` 只处理 procedure,其余原样存入,所以旧数据静默失效。

处置(不改镜像文件):

1. **写入边界归一**:`_canonical_memory_kind` 把 `identity/fact/requested_memory`
   映射成 `profile`(与旧 `MemoryRuntime._canonical_memory_type` 同一张表),
   新写入不会再产生不可注入的行;
2. **doctor 报出存量**:`coremem.non_injectable_types` 列出 active 里的非规范类型,
   当前工作区报 `{'identity': 1}` —— 这类数据以前完全不可见。

这一条是"在线检验设计意图"的直接价值:功能测试会通过(检索确实返回了记录),
只有对着**设计意图**看才发现"召回了但没用上"。

### 13.1 存量修复的闭环验证(F,2026-07-27)

`memory repair-kinds` 把两条 `identity` 归一成 `profile`(自动备份
`20260727-071229-repair-kinds-…`),随后同一个问题重跑:

| | 修复前 | 修复后 |
| --- | --- | --- |
| 召回 | 1 条,score 0.5818 | 2 条,同一条仍是 score **0.5817** |
| 注入 | **False** | **True,363 字符** |
| 模型回答 | "我是 Kirakira,认真又带点活泼" | "哼,既然你诚心诚意地问了…我是**月火**" |
| doctor | `{'identity': 1}` | `non_injectable_types: {}` |

分数没变、召回没变,**只有注入变了**——证明问题确实在类型而不是相关性,
修复也确实落在那一环上。

## 14. 架构迁移后的隔离在线验证（F，2026-08-04）

命令：`uv run kirakira-verify-online`。它读取已保存配置但不打印密钥，所有运行态写入临时
workspace，结束后删除。

| 能力 | 实际结果 |
| --- | --- |
| DeepSeek 文本 | `deepseek-v4-flash` 按要求返回探针文本 |
| 具名强制工具 | 返回 1 个 `online_probe`，JSON 参数为 `value=probe-ok` |
| 用量 | input/output 均返回，coverage=`exact`；本次文本探针 17 input / 7 output |
| Embedding | 真实 OpenAI-compatible endpoint 返回 1024 维向量 |
| Runtime 工具闭环 | 模型选择 `read_file`，Runtime 执行，最终回复消费文件 sentinel |
| 上下文治理 | 预置 60 条持久消息；请求降级到 0 条历史投影后成功；原 60 个消息 ID 顺序与内容未变 |
| Akasha v1 | engine 路由为 `akasha`；真实 turn 摄入后召回 3 条；最终模型正确消费召回代号 |

这轮同时固化了可重复脚本 `scripts/verify_online.py`。它验证的是 provider/runtime/记忆链路 smoke，
不替代 LongMemEval/PersonaMem 的质量评分。

补充勘误：第 12 节记录了当时“JSON 是 canonical”的中间状态；当前 `sessions.db/messages` 已是唯一
权威，JSON 只作为旧导入源/非权威镜像。
