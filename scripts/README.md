# 维护入口

- `check_upstream_kernel.py`：校验完整原生来源摘要及明确记录的补丁。
- `migrate_legacy_workspace.py`：只读旧库，生成独立的新工作区。
- `message_wake_scenario.py`：真实 SQLite 的类型订阅、并发提交、回滚和重开。
- `scheduler_tools_scenario.py`：确定性模型对端上的原生调度集成验收。
- `ledger_runtime_scenario.py`：提交后进程退出、恢复与插件局部更新。
- `clean_workspace.py`：清理缓存，保留源码、环境和用户运行数据。
- `build-web-plugins.mjs`：构建原生插件的工作台界面。
- `stage-web-assets.mjs`：将构建结果放入对应插件制品。

运行 `uv run python -m scripts.clean_workspace` 清理派生缓存与打包产物。
会话、记忆、模型缓存、`.venv` 和 `node_modules` 不属于可自动删除的垃圾。
运行数据使用仓库外的工作区；历史评测结果由 Git 保留，当前结果统一进入
`docs/operations/`，不在根目录或 `eval/results/` 另留副本。

完整运行说明见 `docs/handbook/start.md`；在线 Step5 验证入口为 `eval.native_runtime`。
