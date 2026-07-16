# Handbook 是什么，为什么作者说它非常有用

Reference 项目（akashic-agent）根目录下有个 `_handbook/`，七个 markdown 文件。作者说它非常
有用——这话值得认真对待，但要说清楚"有用在哪"，得先看它**不是**什么。

## 1. 它不是 API 文档，也不是 README

先看一段真实的 handbook 开头（`_handbook/drift-guide.md`）：

> ## 先理解它是什么
>
> Drift 是一个**你写模型可以做什么、模型照着执行**的后台任务系统。
>
> - **什么时候跑**：proactive 拉了一圈啥也没有（无 alert、无 content、无 context fallback）
> - **做什么**：你写在 `drift/skills/<skill-name>/SKILL.md` 里的事
> - **跟 proactive 的本质区别**：proactive 的行为是代码里写死的 system prompt，drift 的行为是你写的 SKILL.md

注意这里没有一个函数签名、没有一个类名、没有一行 `def`。它回答的是四个问题：

```text
这是什么 → 什么时候会发生 → 它会做什么 → 它和旁边那个东西有什么区别
```

对比一下三种文档的分工：

| | 回答的问题 | 读者 | 过期的方式 |
| --- | --- | --- | --- |
| README | 我怎么把它跑起来 | 新来的人 | 命令变了 |
| API 文档 / docstring | 这个函数怎么调 | 正在写代码的人 | 签名变了 |
| **Handbook** | **这个子系统的心智模型是什么、它的规矩是什么** | **要用它、改它、或者被它坑到的人** | **设计变了** |

**最后一列是关键。** API 文档会因为改个参数名就过期，所以大家懒得写；handbook 只在**设计**
变的时候才过期，而设计变化的频率低得多——低到值得手写、值得认真写。

## 2. 它真正解决的问题：把"只在作者脑子里的东西"写下来

一个子系统跑起来之后，代码里能看到的是**当前的实现**。看不到的是：

- 为什么是这样，而不是那样（**被否决的方案**）
- 哪些行为是刻意的约束，哪些只是恰好这么实现（**不变量 vs 巧合**）
- 出错时会怎样（**失败语义**）
- 什么东西**故意没做**

这些东西全在作者脑子里。**Handbook 就是把它们倒出来的地方。**

看 `_handbook/drift-guide.md` 里的这段，它整节叫"Drift 的核心约束"：

> 1. **每次重新选择**：不默认继续上次的 skill，每轮重新比较所有 skill
> 2. **message_push 是 fire-and-forget**：最多推送一次……Drift 不保存"等待回答"，也不能推断"用户没回"
> 3. **必须 finish_drift**：执行结束前必须调用
> 4. **message_result 由 runtime 记录**：调用过 `message_push` 就是 sent，否则是 silent，不由 skill 自报

第 2 条和第 4 条特别有代表性。**它们是"你不要试图做什么"**：

- "不保存等待回答" —— 这不是没实现，是**故意的**。看代码你只会看到"没有这个功能"，
  永远猜不到作者想过并且拒绝了它。
- "不由 skill 自报" —— 这是在防一类具体的错误：让被约束者自己汇报是否遵守了约束。

**这两条如果没写下来，下一个人（包括三个月后的作者本人）一定会"顺手加上"，然后引入 bug。**

再看 `_handbook/workspace-mcp.md` 里的失败语义：

> 任一声明无效或 server 连接失败时，整批候选被拒绝，旧 generation 继续服务；修复文件后 watcher
> 自动重试。删除全部 `.toml` 或整个 `servers` 目录会原子发布空 generation，并排空旧 MCP 进程。

三句话讲清了：失败会怎样、恢复会怎样、边界情况（全删）会怎样。**这是契约，不是描述。**
读代码要读三个文件才能拼出这三句话，而且很可能拼错。

## 3. 最值得学的一点：它记录"什么已经不存在了"

这是我这次更新 reference 时被结结实实上了一课的地方。

`_handbook/plugins-tutorial.md` 第一句：

> Akashic 插件采用"全局只管启停，插件自己声明能力"的模型。插件仓库必须提供根目录 `plugin.py`，
> **不再读取 `.aka-plugin/plugin.json`、`manifest.yaml`、`mcp/servers.json` 或 `registry.json`**。

`_handbook/workspace-mcp.md` 第一句：

> 运行时按内容 revision 热重载，**不再读取 `mcp_servers.json`，也不提供 `mcp_add`、`mcp_remove`
> 或 `mcp_list`**。

**注意它们都在讲"已经没有的东西"。** 从代码里你永远看不到这个——代码只有现在有什么。而这
恰恰是最容易害人的信息：

我们这个项目照着旧版 reference 抄了 `.aka-plugin/plugin.json` 和 `mcp_add`。半年后我更新
reference，如果只读代码，我会看到一堆新文件，然后疑惑"我们的 plugin_manifest.py 对应哪个"。
**是这两句话直接告诉我：你抄的那个东西，上游已经删了。**

> **教训**：文档写"现在有什么"是基本功；写"曾经有什么、为什么没了"才是高手。
> 后者的读者是**从旧版本迁移过来的人**——而这个人往往就是半年后的你自己。

Reference 甚至为此专门写了一个文档：`_handbook/programmatic-control-migration.md`，
标题就叫"迁移"，内容是旧配置 → 新配置的对照，末尾还有一句：

> 旧字段会在配置边界明确失败，不会被静默忽略。

**文档承诺 + 代码兑现。** 这就是第 5 节要讲的"契约"的意思。

## 4. 为什么它不会烂掉：和代码同一个 commit

我查了 reference 的 git 历史，这是最关键的发现：

```text
dcee6f1  refactor(mcp): replace registry with hot declarations (#120)
         └─ 同一个 commit 里新增了 _handbook/workspace-mcp.md

89ceaf2  refactor(control): replace TUI IPC with app server (#118)
         └─ 同一个 commit 里新增了 _handbook/programmatic-control-migration.md

3b456e7  feat(proactive): 引入事件流唤醒与插件包架构 (#109)
         └─ 同一个 commit 里改了 _handbook/proactive-guide.md
```

**handbook 不是"做完之后补的文档"，它是改动的一部分。**

这一条解释了为什么大多数项目的文档最后都变成谎言：因为写文档是**另一个任务**，而另一个任务
永远排在后面。当文档和代码在同一个 commit、同一个 PR、同一次 review 里，它就不可能悄悄过期——
reviewer 会看到 handbook 没改。

> 这是个**流程设计**问题，不是**自律**问题。指望自己"记得更新文档"必然失败；
> 把文档放进改动的定义里就不会。

顺带一提 `_handbook` 那个下划线前缀：它让这个目录在文件列表里**排到最前面**。一个很小的
设计，但传达了"先读我"。

## 5. Handbook 的写法总结

从 reference 那七个文件里能提炼出的固定套路：

### 5.1 结构

```text
1. 这是什么（一句话，用大白话，不用术语）
2. 什么时候会发生 / 谁触发它
3. 心智模型（一张 ASCII 树或流程图）
4. 核心约束（编号列出，包含"不要做什么"）
5. 真实例子（能直接抄的完整配置 / 代码）
6. 失败语义（错了会怎样、怎么恢复）
7. 已经不存在的东西（如果这次是重做）
```

### 5.2 一句话开头，必须是大白话

对比这两种写法：

```text
✗ DriftTurnPipeline 负责在 proactive gateway 返回空结果时编排 skill 选择与原子动作执行
✓ Drift 就是：没新闻可推的时候就干点后台活儿
```

第一句是给已经懂的人写的（也就是没用）。第二句才是给不懂的人写的。

reference 里几乎每个 handbook 都有这样一句"人话版"。`drift-guide.md` 甚至直接写了：
"简单说：**没新闻可推的时候就干点后台活儿**"。

### 5.3 用 ASCII 图画心智模型，不画类图

```text
┌─ proactive gateway 无 alert / content / context
│  └─ DriftTurnPipeline
│     ├─ 扫描并比较 drift skills
│     ├─ select_skill 或 idle_drift
│     └─ 执行一个原子动作
│        ├─ 可选 message_push（最多一次）
│        └─ finish_drift
└─ done
```

这张图画的是**发生了什么**，不是**有哪些类**。类图会因为重构过期，"发生了什么"不会。

### 5.4 约束要编号，而且要写"不要做什么"

编号是为了能被引用（"违反了 drift 约束第 2 条"）。而"不要做什么"是 handbook 独有的价值——
**代码只能表达"是什么"，表达不了"我考虑过 X 并且拒绝了它"**。

### 5.5 失败语义必须写

> 任一声明无效或 server 连接失败时，整批候选被拒绝，旧 generation 继续服务

这句话让读者知道：改坏了配置文件不会搞挂正在跑的 agent。**没有这句话，没人敢动那个文件。**
文档的一个核心作用是**给人动手的信心**。

## 6. 那我们应该怎么做

结论：**给 kirakira 建一个 `_handbook/`，并且从现在开始，改设计就改 handbook，同一个 commit。**

跟本项目现有 `docs/` 的分工：

| 目录 | 定位 | 读者 |
| --- | --- | --- |
| `README.md` | 怎么跑起来 | 第一次接触的人 |
| `_handbook/` | **各子系统的心智模型与契约** | **要用/要改这个子系统的人** |
| `docs/VERSION_EVOLUTION.md` | 演进史：为什么变成今天这样 | 想学工程演进的人（你） |
| `docs/ARCHITECTURE_LESSONS.md` | 可迁移的架构判断 | 想学架构的人（你） |
| `docs/DIFFERENCE_AUDIT.md` | 与 reference 的差异台账 | 做同步的人 |

注意 `_handbook/` 和 `docs/` 是**不同性质**的东西：

- `docs/VERSION_EVOLUTION.md` 是**历史**——它记录"曾经"，所以越写越长，不会过期（过去不会变）。
- `_handbook/` 是**契约**——它描述"现在"，所以必须跟着代码改，长度稳定。

**别把这两种混在一个文件里。** 混了之后，读者不知道哪句是历史哪句是现状，而这正是绝大多数
项目文档变得不可信的原因。

本次已经开了个头：`_handbook/workspace-mcp.md`（对应我们刚做完的声明式 MCP）。
建议接下来补的顺序，按"不写会出事的程度"排：

1. `_handbook/snapshot-and-lease.md` —— 代际与租约的规矩。**最该写**：
   "子任务必须 fork 租约"这条约束不写下来，下一个人一定会踩。
2. `_handbook/plugins.md` —— 插件怎么用代码声明能力，以及描述符文件已经没了。
3. `_handbook/memory.md` —— 五个 Markdown 文件各自的写者与读者，consolidation 时序，
   以及"写入侧向量失败会报错"这个契约。

## 7. 一个检验标准

写完一份 handbook，用这个标准检验：

> **把这个子系统的代码全删掉，只留 handbook，一个熟练工程师能不能重新实现出一个
> 行为等价（包括失败行为）的版本？**

- 能 → 这是一份好 handbook。
- 不能 → 缺的那部分，就是"只在你脑子里"的部分，也正是最该写下来的部分。

这个标准听起来很高，但 reference 的 `workspace-mcp.md` 基本做到了——我这次实现声明式 MCP
时，`declarations.py` 的校验规则（文件名必须等于 name、cwd 相对声明文件解析、越界拒绝、
revision 取内容哈希）**有一大半是从那份文档里读到的，而不是从代码里读到的**。

这就是作者说它"非常有用"的意思。
