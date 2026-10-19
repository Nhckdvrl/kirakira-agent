# ADR0015：项目默认值与会话工作目录

日期：2026-10-19。状态：当前本地实现生效。

参考 Akashic 固定提交
[`21b38f0e102d`](https://github.com/kachofugetsu09/akashic-agent/tree/21b38f0e102d9e1bdc5576725635dce29f135c0c)，
主要合同来自 [Project/cwd 设计](https://github.com/kachofugetsu09/akashic-agent/blob/21b38f0e102d9e1bdc5576725635dce29f135c0c/docs/design/project-working-directory.md)、
[目录 owner](https://github.com/kachofugetsu09/akashic-agent/blob/21b38f0e102d9e1bdc5576725635dce29f135c0c/plugins/working_directory/working_directory.py)
和[工具入口](https://github.com/kachofugetsu09/akashic-agent/blob/21b38f0e102d9e1bdc5576725635dce29f135c0c/plugins/working_directory/directory_tool.py)。

## 问题与结果

过去 Session 的文件与 Shell 都使用 runtime workspace。多项目会话无法选择各自的代码目录，
子任务也不能继承父会话实际操作的位置。现在 runtime workspace 继续存放会话、配置和运行状态，
代码目录归每个 Session 独立持有；Project 只在实际创建 Session 时提供一次默认值。

## 所有权与事务

- `plugins/projects/service.py` 拥有项目 ID、名称、归档和一次性默认目录绑定。
- `plugins/working_directory/service.py` 拥有 Session 目录、修订、切换回执及实时仓库材料。
- `session/owner_store.py` 在 `sessions.db` 提供带版本的 owner 记录，不另开一份业务数据库。
- `bootstrap/workspace.py` 装配两个 owner，发布 `projects.v1` / `working_directory.v1`，注册新 Session initializer。

`SessionManager.get_or_create` 在同一 `BEGIN IMMEDIATE` 事务内检查项目、插入新 Session、
冻结默认目录并运行全部 initializer。后续 initializer 失败时，Session 和 owner 行一起回滚，
不会发布部分缓存对象。已存在的 Session 从不再次运行 initializer；缺失目录行表示旧的 unset 状态。
这只是创建事务的新增能力，不表示已经移植完整上游 Ledger。

项目使用稳定 `p_` 加 32 位小写十六进制 ID。原创建参数重试读取原记录，即使项目已经改名、
归档或目录已消失；相同 ID 携带不同原始参数则冲突。项目只改名和归档，不删除历史身份。
默认目录可在创建时设置，或从空值绑定一次；归档只阻止新 Session，已有会话保持可用。

## 目录切换与执行

切换要求已有目录。绝对路径独立解析；相对路径必须依赖当前有效 cwd。
目录丢失后，依赖它的文件和 Shell 操作报错，不退回 runtime workspace，不重新创建丢失的 worktree。
显式绝对文件目标仍可访问。没有配置 cwd 的旧会话继续使用原工作区文件工具行为。

目录状态和稳定 effect key 的回执在同一事务内提交。重试先查回执，再检查磁盘；
回执重用时参数必须一致。不同目标使用版本比较，同路径切换不推进修订。
控制端必须携带读到的 `expectedRevision`，其中 `null` 明确表示预期目录记录仍不存在。
工具调用身份由实际执行器绑定，模型不能传另一个 Session ID 来切换别人的目录。

`set_working_directory` 声明 `exclusive_batch`。混合批次整体拒绝，任何一个调用都不产生效果；
后续一批才能操作新目录。外部控制切换等待共享 admission，不能穿过正在执行的 turn。
已经启动的进程固定其启动 cwd；目录切换只影响后续执行。子任务在创建自身 Session 时冻结父目录，
之后独立切换，不反向修改父会话。

## 实时仓库材料

每次新模型生成读取当前 cwd 的仓库规则。最近的 `.git` 文件或目录界定根，按根到 cwd 顺序读取；
同层 `AGENTS.override.md` 优先于 `AGENTS.md`，总预算 32 KiB。
读取失败、编码损坏或超预算时整链标为 unavailable，不继续沿用旧规则。
模型收到暂停依赖这些规则的仓库写入提示，对话仍可继续。

规则只作为当前请求材料；切换后替换原材料，并同步刷新 compactor 的 system prefix。
工具结果仅保存路径和规则来源等元数据，不把规则正文变成会话历史事实。
同一次模型 I/O 的网络重试固定本次材料。仓库规则不授予新权限，也不是租户安全边界。

## 入口与范围

Web `/workspace`、JSON-RPC 和 Agent 工具共用相同服务及数据。
Web 项目聊天将项目 ID 放入会话路由与创建元数据，不另起一个项目专属 runtime。
当前采用本机文件系统和执行管理器；尚未移植 HostBridge、prepared 工具恢复、完整工作台和移动端。
项目不会自动切分记忆图或形成多租户隔离。

迁移只新增 `20261019_02_workspace_owners.py`，不改已有迁移，不回填旧 Session，
不修改原始消息或记忆算法。实际功能证据见[验证报告](../operations/evaluations/2026-10-19-workspace-functions.md)。
