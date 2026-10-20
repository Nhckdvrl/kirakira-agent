# 本地工作区迁移验证（2026-10-20）

本地既有数据已转换到原生工作区，并已切换仓库默认 config.toml。
运行数据位于 /var/tmp/xiang-kirakira/workspace（fvcrc10 本机 XFS），
~/.kirakira/workspace 指向该目录。Python 继续使用项目唯一 .venv / 3.14.6。

## 数据与入口

| 项目 | 结果 |
| --- | --- |
| 历史消息 | SQLite 96 + JSON 16 = 112 条；旧索引 16 条均已去重 |
| 会话 | 22 个，原始消息 ID、时间、正文及附加出处保留 |
| 长期记忆 | 2 条迁入原生 Markdown 档案，原始条目另存私有快照 |
| Markdown | 5 份原文转入 topics/，旧 PENDING 不作为新队列重放 |
| Skill | 6 个迁入工作区；其中旧插件、MCP、Codex 和技能创建文档已适配 |
| 工作目录 | 22 个旧会话均固定到原项目目录 |
| 模型 | 默认 Step5，专用 step-plan 驱动及 Token Plan 地址 |
| 旧外部渠道 | 均未启用，迁移保持停用 |
| 原生入口 | main.py gateway + main.py exec 真正经 SDK/JSON-RPC 完成回复 |

完整检查未发现非空旧项目、任务、MCP 或向量图状态。旧 memory2.db 的记忆表为 0 条；
现有两条有效记忆在 items.json 中。跨会话记忆验证覆盖迁入 Markdown 档案；
本次没有配置 embedding 模型，不将该结果表述为 Akasha 图学习或向量召回的验收。

旧地址转换为工作台可访问的 akashic:legacy: 地址，mapping 保存在私有 migration.json；
消息身份保留。导入的事实不会重新取得历史执行授权，旧工具链文本和引用仍可追溯。

## 安全切换与备份

迁移器取得旧 Runtime 的实例锁，通过 SQLite backup API 包含已提交 WAL，
并对全部源文件做快照后、切换前的一致性检查。同序消息冲突、运行中实例、
新增事实、未支持的非空状态都会阻止发布或入口切换。
只有原生启动、历史读回和真实 Step5 smoke 通过后才原子替换入口配置。

首次尝试发现共享 NFS 上的 SQLite WAL 访问停滞，未切换入口。
正式工作区改用本机 XFS 后启动和完整调用成功；原生内核没有因此修改。
旧 Supervisor 锁未被强行删除。原始数据仍保留，失败尝试的重复运行文件已清理。

共享备份目录为 ~/.kirakira/backups/：

- legacy-before-native-20261020：19 个原始数据文件及摘要清单。
- native-workspace-20261020：60 个迁移后持久文件及摘要清单，包含原生选择、事务库与凭据存储。

两份备份和活动工作区均私有；备份逐文件校验，SQLite 完整性检查通过。
运行端点、socket、PID 和锁文件在恢复时重新生成。
回退旧实现需先停止新实例，使用迁移前提交 65c8181，再恢复旧配置；原始数据没有被覆盖。
完整原生快照可恢复到本机数据盘并由当前实现读取。

## 验证结果

93 项测试全部通过。真实工作区的以下检查通过：

- 全部旧会话目录状态、旧对话继续回答、独立新会话读取迁入记忆。
- 相对路径 write_file / read_file、原生 Skill 目录。
- 工作台页面和会话目录 HTTP 200，全部迁入会话出现在目录中。
- 正式 Gateway、发行 SDK 和 CLI 完成 Step5 回复。

业务模型账本记录 9 次成功调用，均为 step-plan，地址均为
https://api.stepfun.com/step_plan/v1；148371 输入 token、544 输出 token。
连接与模型配置探测不计入该业务调用计数。

924 个固定原生生产文件继续通过校验。wheel/sdist 构建通过；
SDK 与迁移模块逐字核对，sdist 的 924 个原生文件与源码一致。
在隔离 wheel 路径上导入正式 Gateway 与 SDK 通过；包内真实密钥匹配为 0。

[汇总](../artifacts/2026-10-20-workspace-migration/summary.json)、
[功能](../artifacts/2026-10-20-workspace-migration/functional.json)、
[打包](../artifacts/2026-10-20-workspace-migration/package-checks.json)、
[测试](../artifacts/2026-10-20-workspace-migration/unit.log)。

迁移器的其他旧格式需要逐一实现显式转换；遇到非空未知状态会拒绝迁移。
这不影响当前本地已完成的迁移，也不表示任何未知旧工作区都已具备自动转换器。
