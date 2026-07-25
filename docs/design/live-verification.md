# 实弹验证记录:哪些链路真的跑过

- 状态:accepted;第 2–6 节为 2026-07-25 一轮,第 7 节为 2026-07-26 控制面一轮
- 核对基线:`Reference/` @ `012e37c8b51df045353972bb551d8e868ab52455`
- 目标读者:维护者、评审者、接手做下一轮验证的人
- 关联:[NOW.md](../NOW.md)、[decisions/0004](../decisions/0004-delivery-dedup.md)

标注:**F** 已实际执行并观察到结果;**G** 尚未验证。

## 1. 为什么单独记这一份

离线回归 454 passed 只说明**单测口径**下的行为成立。它抓不到两类问题:

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
- 修法:`coremem/compat_worker.py` 在 kirakira 边界容错,镜像文件保持与 Reference 逐字节一致
  (doctor 漂移审计 16 个文件 `drifted=[]`);
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
| 插件声明的 MCP 主动源端到端 | 只用替身 gateway 验过编译与注册,没有真实 MCP server 的 fetch/ack |
| Web / QQ / 官方 QQBot 渠道 | 只有 Telegram 做过真实投递 |
| 热重载与在途 turn 的竞争 | 代际租约有单测,但没在真实并发下观察过 |
| 跨崩溃去重的真实崩溃场景 | 用重开库模拟过,没有真正 kill -9 之后重启验证 |
| 长时间运行 | 没有连续跑数小时观察内存、连接与调度漂移 |
| 控制面 TCP 模式与 token 认证 | 只验过 Unix socket 无 token 路径 |
| `plugin/disable-and-drain` 真实排空 | 当前 workspace 没装插件,只验过错误路径 |
