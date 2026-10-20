# 评测

`python -m eval.native_runtime --key-file FILE --workspace NEW_DIR --output REPORT.json`
使用完整生产插件图和 Step5 Token Plan。每次使用新目录，模型响应不替换为夹具。
`eval/fixtures/native_extension` 提供真实托管 HTTP 进程和 stdio MCP 服务，不连接外部收件人。

原生确定性验收：

```sh
python -m scripts.message_wake_scenario
python -m scripts.scheduler_tools_scenario --output /tmp/new-scheduler-contract
python -m scripts.ledger_runtime_scenario
```

Scheduler 场景使用本地确定性模型 HTTP 对端，验证工具权限、调度来源、交付、用户抢占与取消。
它与真实 Step5 评测分开记录。

当前量化结果及原始证据统一放在 `docs/operations/evaluations/` 和
`docs/operations/artifacts/`。旧主链的评测程序与过期结果只保留在 Git 历史中。
新运行使用仓库外的临时工作区和输出路径，正式记录再纳入对应评测文档。
