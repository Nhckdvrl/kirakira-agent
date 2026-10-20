# Runtime 架构

Kirakira 的执行主干移植自 Akashic `d8761bb9000a7be8701c65e049b0978879cc6889`。
完整来源与摘要在根目录 `UPSTREAM.json`，许可证在 `UPSTREAM-LICENSE`。
CLI、初始化、历史导入、Step Token Plan 驱动与本地评测位于这份清单之外。

```mermaid
flowchart TD
    CLI[CLI / Supervisor] --> Manager[PluginManager]
    Manager --> Root[CompositionRoot / Context / Fiber / Effect]
    Root --> Host[Task / ExecutionAccess / HostInfo]
    Channels[Channels / Gateway / Programmatic] --> Sources[Sources / SourceSession]
    Sources --> Ledger[Ledger / MessageReader / MessageWriter / OwnerState / Bindings]
    Ledger --> Reply[Reply follower]
    Reply --> Program[ReplyProgram]
    Program --> Context[Context / Materials / Compaction]
    Program --> Models[Models / Bound driver / Call receipts]
    Program --> Tools[Tool menu / Prepare / Authorize / Execute]
    Tools --> Execution[Files / Processes / HostBridge / MCP / Workloads]
    Program --> Ledger
    Ledger --> Delivery[Delivery policy / Sender / Durable receipt]
    Ledger --> Consumers[Akasha / Jobs / Eventmail / Wake / Drift / Scheduler]
```

## 宿主和生命周期

`bootstrap/tools.py` 只装配宿主资源与 PluginManager。业务数据库和后台工作归普通插件。
PluginManager 固定来源制品、配置输入和公共合同，在同一个 live Root 中实施局部更新。
`contract.py` 的公共类型在进程内保持身份；私有业务模块按 generation 隔离。
外部调用模型设置等业务入口使用公开 JSON RPC，不传递其他 generation 的私有 dataclass。

Context 授权依赖和资源声明。Fiber 拥有 activation，Effect 拥有撤销和物理排空。
Task 服务按真实插件 owner 保存；来源使用 `(session_id, source)` 进行任务接纳。
任务撤权与底层工作物理退出分别处理，关闭会等待实际清理。

## 消息、权限和恢复

Ledger 是唯一消息事实库。Input、Output、ToolCall、ToolResult 和 Control 都进入有序消息日志。
Reader 只有读取权；Writer 固定 author、source、body 类型、内容校验和可选 exact CallRef。
回复任务关闭立即释放新输出写权；已经开始的工具持有自身结果写权直至结算。
OwnerState 将各 owner 的 CAS 状态与消息提交放在同一个 SQLite 事务域。
Bindings 保存实际 provider 来源和固定业务选择，恢复时不会用当前同名工具替换旧授权。

SourceSession 先持久接纳 Input，再撤销旧任务的提交权。
Source head 校验、工具首次效果和最终输出提交共享事务顺序，取消通知不充当正确性依据。
TurnProjection 从日志恢复轮次；Reply 跟随日志并选择程序；ReplyProgram 只组合一次执行。
模型调用的重试、流重置和回执由 Models 统一拥有。工具效果身份来自持久 CallRef。

## 执行与扩展

工具先固定菜单和 binding，再准备参数、授权、提交 intent、执行并写回原调用结果。
执行目标在 prepare 阶段固定。ExecutionAccess 绑定真实 Root、代码 owner 和 generation，
只接纳由该 grant 签发的 PreparedProcess。
Host execution 提供 Files、Processes、Terminals 与 HostBridge；ManagedProcesses、MCP 和
Workloads 分别拥有实际资源及借用生命周期。Workload 的容器 controller 是可选外部设施。

模型、项目、工作目录、技能、主动交互、记忆、投递和界面都以插件声明其能力和实际依赖。
可选提供方缺席时，相应功能分支保持等待或明确不可用。

## Kirakira 装配

`kirakira_agent/runtime.py` 只选择发行版组合、初始化 workspace 并提供 Python 调用入口。
`kirakira_agent/cli.py` 将执行交给原生宿主 CLI。
没有旧 ConversationService、ProgramStore、自建 TurnAdmission 或另一套依赖图参与运行。
Step5 是普通 Models driver，固定订阅端点且不跟随重定向。
它使用的原生 OpenAI 传输代码固定在自己的插件制品内，由同一 owner 的来源摘要覆盖。
Akasha 消息界面由固定的前端源码和 npm lock 重建，生成摘要单独记录。

移植代码保留一处运行修复：zsh 函数快照解析期间关闭别名展开，随后恢复原选项。
补丁和摘要在 `patches/zsh-snapshot-aliases.patch` 与 `UPSTREAM.json` 中。

## 记忆与主动交互

Markdown Memory 拥有显式档案；Akasha 从 Ledger 的完整闭段学习，原文仍由 Ledger 唯一持有。
消息向量进入可重建的 sessions-derived.db；图、消费进度和召回证据进入 Akasha 派生快照。
查询会话可通过原生 SessionAttributes 排除学习，避免评测答案污染历史。
local-embedding 是普通 Models driver，使用本地 ONNX 权重和固定资产摘要，运行时不下载或请求现金 API。

EventMail 分别接纳 Content、Alert、Context；Wake 按原生兴趣池与规则决定是否执行、调查和分享。
Alert 使用真实 Delivery 回执完成结算；Content 不足以触发时记录可观察的 content_insufficient。
event-ingress 只把已认证 Gateway RPC 转到原生绑定来源，不另建事件队列或任务调度器。
Drift 的公开插件保存提案状态并提供 Wake/Delivery 窄合同；当前组合没有空闲提案生成提供方。
不能将其源码存在表述为完整的第二主动生成流程已上线。

运行诊断和插件安装也是原生插件。新工作区默认选中；既有工作区使用显式 upgrade 命令，
保留其余插件选择并备份原选择。各功能启用与验证状态见[差距文档](../status/akashic-gap.md)。

发行构建另有 `patches/stage-web-assets.patch`：npm build 最后把 dashboard/chat 产物移入所属插件制品目录，
避免源码已更新、运行时却读取旧构建。补丁、来源摘要与本地摘要均登记在 UPSTREAM.json。

原生空图重建已登记 `patches/akasha-empty-rebuild.patch`：只允许合法零节点通过既有发布分支，负数仍拒绝。
旧历史的本地向量补算属于显式运维入口，只写 Ledger 派生缓存；Akasha 保持唯一学习与重建 owner。
既有工作区升级通过原生 prepare_plugin_input / PluginSelection CAS 更新已选输入，
不会通过删除稳定选择或重建另一张依赖图来升级。
