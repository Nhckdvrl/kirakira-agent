# Workspace migrations

`core/` 保存宿主持久状态的迁移，`catalog.toml` 声明依赖。
业务迁移放在相应插件的 bundle 中，由 `migration.catalog.toml` 声明源文件、依赖和事务属性，
通过正式安装链发现；Core 执行声明，不为业务插件建立第二份 schema 权威。

新工作区由实际 owner 建立当前结构，Yoyo 保存独立执行账本。
已有成功迁移保留原 ID 和回执，不重新执行，也不伪造“已经升级”的状态。
执行前取得工作区锁；失败阻止启动，修正后可沿正式入口重试。
具体发现、排序和执行见 [runner](../agent/migrations/runner.py)。

已发布迁移保持不可改写。是否可以退役旧脚本，取决于仍支持的升级路径和实际成功回执，
不能只看文件日期、某一台机器的账本或当前默认配置。新迁移不依赖已从源码退役的历史 ID。
删除源码也不授权删除用户工作区数据。

当前格式的工作区按[升级说明](../docs/handbook/memory-proactive.md#升级既有工作区)更新；
早期 Kirakira 配置与数据库使用[显式导入](../docs/handbook/start.md#既有-kirakira-数据)。
一次性切换和已退出支持范围的历史实现通过 Git 查看，不作为当前操作步骤。
