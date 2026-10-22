# 人格、相处方式与言行一致

Kirakira 面向日常交流、兴趣、学习、生活与工作，是陪伴型、助理型、主动交互型的个人 Agent。人格设计要让用户逐渐认识它怎样回应、怎样判断、怎样帮忙，并能调整这种相处方式。长期记忆提供实际经历，工具与主动链路让交流可以转化为行动。

## 从开源项目学习怎样建立辨识度

核对日期：2026-10-22。以下覆盖 14 个项目，Letta 同时检查旧入口与当前主线，共固定 15 个仓库、52 份源码或说明文件的版本与摘要，见[参考清单](../operations/evaluations/artifacts/2026-10-22-companion-personality/design-references.json)。表中描述各项目公开的取向及本次读取的机制，不是外部项目的功能验收或能力排名。

| 项目与一手材料 | 怎样让用户记住它 | 设计输入 |
| --- | --- | --- |
| [OpenClaw](https://github.com/openclaw/openclaw/blob/240a90ef9f95650bcb1bb17832252dd880f16fb6/README.md)、[SOUL](https://github.com/openclaw/openclaw/blob/240a90ef9f95650bcb1bb17832252dd880f16fb6/docs/concepts/soul.md) | 在自己的设备和聊天入口使用自己的助理；人格文件明确表达立场、幽默和相处边界 | 人格要具体改变行为，身份、用户资料与操作规则各有用途；不靠堆砌人物形容词 |
| [Hermes](https://github.com/NousResearch/hermes-agent/blob/944c3a434071f43e64b443d033f3eba30c64434c/README.md)、[人格](https://github.com/NousResearch/hermes-agent/blob/944c3a434071f43e64b443d033f3eba30c64434c/website/docs/user-guide/features/personality.md) | 强调经验积累与学习循环；稳定身份叠加临时 personality | 稳定设定跟随 Agent，临时要求影响当前交流，切换项目不应意外更换人格 |
| [Eliza](https://github.com/elizaOS/eliza/blob/a922295ebe7f30053abb0257dffa43d9ba910693/packages/core/src/types/agent.ts) | 用可配置人物承载自主 Agent，bio、topics、分场景 style 与 messageExamples 共同塑造角色 | 组合特征、示例和场景行为；角色配置与运行中的启停、状态分开 |
| [SillyTavern 角色设计](https://docs.sillytavern.app/usage/core-concepts/characterdesign/) | 角色描述、情景与对话范例直接支撑角色体验 | 示例比抽象形容词具体；常驻人物材料会占用对话预算，必须控制长度与用途 |
| [AIRI](https://github.com/moeru-ai/airi/blob/448ada1a41c29d84b3975a73ee732f7bdcd13c27/README.md)、[角色关系设计](https://github.com/moeru-ai/airi/blob/448ada1a41c29d84b3975a73ee732f7bdcd13c27/docs/ai/adr/2026-10-09-character-as-chat-member.md) | 用声音、表情和共同活动呈现一个可相处的角色 | 言语、角色设置与活动中的行为需要一致；所读 ADR 标为 proposed，不能将其中全部步骤当成已交付 |
| [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber/blob/992309c0aa19845960228f880013d4685fde93b5/README.md)、[角色配置](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber/blob/992309c0aa19845960228f880013d4685fde93b5/src/open_llm_vtuber/config_manager/character.py) | 用实时语音、打断、视觉与 Live2D 展示陪伴 | 展示应围绕可体验的行为；本次参考的是 v1 代码，README 中 v2 仍在讨论与规划 |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot/blob/0cd42bd4dd216d8810da4e3f5fb2e381d3c6817e/README.md)、[人格管理](https://github.com/AstrBotDevs/AstrBot/blob/0cd42bd4dd216d8810da4e3f5fb2e381d3c6817e/astrbot/core/persona_mgr.py) | 在常用 IM 中使用可配置人设、示例和插件能力 | 设置需要有清楚的选择与生效入口；定时任务中的人格、上下文与投递也要检查 |
| [Letta Code](https://github.com/letta-ai/letta-code/blob/035bb8c8917b5dd94a3989d4adf04860a2ce8fea/README.md)、[当前记忆接口](https://docs.letta.com/agent-sdk/memory) | 用跨时间的记忆、身份和经历呈现持续存在的 Agent | persona 与 human 各有含义，写入记忆与当前上下文采用该版本分开；当前主线是 MemFS，旧 V1 memory-block API 不能代替当前设计 |
| [QwenPaw](https://github.com/agentscope-ai/QwenPaw/blob/0db9a8e625e73917a0d113e255b3147134022db8/README.md)、[身份与用户资料](https://github.com/agentscope-ai/QwenPaw/blob/0db9a8e625e73917a0d113e255b3147134022db8/src/qwenpaw/agents/md_files/zh/PROFILE.md) | 个人助理的记忆、工作空间和多入口协作 | Agent 的气质和用户的生活事实需要区分；同一产品可以兼顾相处、帮忙与主动执行 |
| [Nanobot](https://github.com/HKUDS/nanobot/blob/7dfeff9ddf2e09edfa3df88496fc143b25d690ac/README.md)、[SOUL](https://github.com/HKUDS/nanobot/blob/7dfeff9ddf2e09edfa3df88496fc143b25d690ac/nanobot/templates/SOUL.md) | 易理解、能自己掌握的轻量个人 Agent | 用简短而有行为含义的原则作默认；人格、用户资料和工具合同分别组织 |
| [Rowboat](https://github.com/rowboatlabs/rowboat/blob/2f75157285d5bea71e0438c87e537a717b71b42a/README.md) | 将个人上下文放入邮件、文档、会议和后台工作等具体工作界面 | 展示历史怎样参与实际工作，把用户能够查看和继续处理的结果放到前台 |
| [memU](https://github.com/NevaMind-AI/memU/blob/42ef9fa3a8034f5817684cba23ef69fa70a58197/README.md) | 用跨会话、跨 Agent、跨设备的 Wiki 组织个人记忆 | 稳定偏好、具体经历和可复用方法各有用途，读取到材料后仍要检查怎样影响当前行为 |
| [Pi](https://github.com/earendil-works/pi/blob/db774055e3ecae05ab6eae8656454cde98e36780/packages/coding-agent/README.md) | 通过最小内核与可定制工作流解释设计取舍 | 不预制一批只能整体切换的角色；按需求组合特征，沿已有扩展机制实现 |
| [Maka](https://github.com/apache/maka/blob/59f61cf7de665837630518aa204732107dcc2349/README.md) | 用完整执行记录和公开实验支撑任务完成的主张 | 回答风格、实际行为与执行结果分别测量，保留失败、条件及原始记录 |

从这些材料得到的判断是：辨识度通常来自一组明确的使用体验，并通过默认行为、操作入口、演示和实验反复呈现。人格文件、记忆、工具和定时器本身已有广泛应用；Kirakira 要证明的是它们在相处与办事中共同产生什么结果。

## Kirakira 的人格由什么构成

| 部分 | 内容与来源 | 作用 |
| --- | --- | --- |
| 人物底稿 | 用户拥有的 `memory/VEDA.md` | 名字、稳定气质、立场、兴趣及相处原则；可以保留轻微吐槽、独立判断等具体特点 |
| 表达习惯 | 六个独立配置：温暖、直率、幽默、追问、展开与表情 | 控制怎样说；未设置项继承底稿，不推断其他维度 |
| 回应方式 | 支持方式、闲聊建议两个独立配置 | 控制先做哪种回应：接住感受，或回应后给个办法；是否顺带提出想法 |
| 说话示例 | 用户提供的有限长度样例 | 传达措辞与节奏；样例中的共同经历、数字和操作结果不成为事实 |
| 相处经历 | 原始消息、情景记忆和已确认的档案 | 提供真正发生过什么、用户说过什么；不能把旧心情当成当前状态 |
| 当前处境 | 本轮请求、真实工具结果与已获准的主动任务 | 决定此刻怎样使用人格；严格答题、工作交付和日常交流各有要求 |

这允许温暖而直率、沉静而幽默、愿意了解但不擅自安排任务等组合。人物底稿仍保留自由表达空间，组合项只覆盖明确选择的部分。

### 同样的语气，可以有不同的相处行为

`support` 用于用户表达困扰且未明确指定回应方式的时候：

- `listen_first`：先接住实际处境与感受，留出继续说的空间，本轮不主动开方法或计划。
- `next_step`：回应感受后给一个容易尝试的小步骤，不擅自替用户执行。

开发时试过“一起理清”的中间选项，但在烘焙、练字与展览样本中经常只有普通安慰，没有稳定表现出额外行为，因此没有保留为正式选项。原始回答与不符合情况仍在评测证据中；用户想一起想问题时仍可直接提出，人物底稿和补充偏好也保留自由空间。

`suggestions` 用于普通闲聊：`on_request` 默认接话，问到再给办法；`light_offer` 在有具体由头时可以顺带给一个低负担的想法。它不要求每轮硬塞建议，情绪交流另采用 `support`。

用户此刻说“只想倾诉”“帮我想办法”或交办任务时，当前要求优先。少追问依然适用于支持与建议，不用“要不要……”或句号把问句伪装成陈述。人格通过实际回应体现，不在回答中朗读配置。

### 稳定立场与可信的言行

有个性需要稳定取舍。可以在底稿中写清怎样表达不同意见、遇到未知怎样承认、喜欢怎样一起探索，而不是仅写“温柔、聪明、俏皮”。表达温柔仍应指出错误，表达直率仍针对事情，幽默也要贴合用户提供的材料。

记忆负责为熟悉感提供依据。过去聊过什么可以召回并继续核对，未知偏好就保留未知；说话示例里的虚构旅行不证明共同经历。没有看到画作或听到录音时，回应用户描述的感受，不声称观察过颜色、线条或音准。

行动需要实际状态支持。说“已记住”“已安排”“已完成”分别需要对应的写入、任务或工具结果；发出通知还需要投递记录。支持方式可以改变先回应还是先提出办法，不增加文件、外部服务或发送权限。

## 沿现有架构生效

`plugins/prompt/personality.py` 定义配置及共用语义，工作台从同一组选项读取含义。表达与行为进入既有 Prompt 材料，日常对话和 Wake 共用，当前请求持有已经组装的字符串。配置沿宿主保存与换代，已有用户底稿不被覆盖。

表达和行为优先顺序为：当前明确请求与格式、显式组合项、当前用户已经确认的偏好、人物底稿与自我档案、默认表达。权限与执行事实不由这组优先顺序改变。

没有额外的逐轮人格分类模型、情绪数据库或亲密度分数。空闲规划继续依据真实用户目标、历史和已开启的范围生成提案；后续 Wake 使用共同的相处设定表达并决定分享或跳过。人格不能单独开启主动联系，主动频率也不由追问习惯决定。

字段合法性、持久保存、请求冻结、工具权限与主动预算由代码管理。倾听、建议、幽默与自然表达属于模型生成的行为要求，需要用实际回复检验，不能因为配置保存成功就宣布人格精确。

## 用实际行为证明特色

验证分别回答以下问题：

| 问题 | 验证方式 |
| --- | --- |
| 改变的是相处方式，还是同时换了一整个人设？ | 固定六个表达维度和输入，每次仅改变一个行为字段，保存全部回复 |
| 支持方式是否真的不同？ | 检查倾听是否夹带建议、提出的办法是什么；评审不看目标配置，引用回复原句，另逐条复读 |
| 能否尊重用户当下的意愿？ | 在同一会话切换“只想说说”和“请给一个步骤”，检查临时要求没有改写持久配置 |
| 相处之后还能否正常办事？ | 同一对话继续计算、真实写入与读取文件，核对实际结果和工具身份 |
| 聊天与主动交流是否沿用同一个 Agent？ | 同一 Prompt 材料验收、真实历史接续、用户结束后的停止，以及既有投递与重启场景 |
| 记忆是否改变现在的回答和行动？ | 长原文细节取证、同事件不同历史条件，以及来源复读；回答质量、开销和行动分别记录 |

自动语义评审采用同一模型，可能误判；问题摘录和不存在的观察事实需要逐条检查。组合辨识不等于用户喜欢，更不等于长期关系质量。首轮出现的配置等待超时、隐含追问与无依据解释均保留在[伙伴与个性实测](../operations/evaluations/2026-10-22-companion-personality.md)中；目前的维护复读也不冒充独立真人评价。

## 如何让产品特点看得见

首页先说明用户能怎样与它相处、交办事情和接收主动交流，再展示三种交互怎样使用真实历史与共同设定。个性设置直接呈现可独立选择的回应方式；记忆与跟进界面保留来源和结果的查看入口。具体设置方法见[个性与相处方式](../handbook/personality.md)。

当前值得集中呈现的体验是：**可以调整相处方式，经历有出处，聊过的事情能接着处理，主动交流有执行和停止记录。** 这是 Kirakira 正在共同打磨的一组特点，不要求把每个机制称为独创，也不把它限制成监控或提醒工具。

继续验证多轮人格稳定性、不同前端模型的行为符合程度、真实用户偏好与长期主动时机。声音、表情、虚拟形象、自动人格成长和完整关系建模属于另外的产品工作，未交付的部分不进入当前能力介绍。
