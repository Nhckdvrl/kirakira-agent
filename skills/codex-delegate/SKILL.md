---
name: codex-delegate
description: 用户明确要求通过本机 Codex CLI 执行代码任务时使用。
metadata: {"skill":{"requires":{"bins":["codex"]}}}
---

# 使用本机 Codex CLI

先通过 shell 检查 command -v codex、codex --version 和 codex exec --help。
沿用用户授权的仓库与修改范围。长任务使用 shell 的 timeout、yield_time_ms；
若返回执行句柄，使用 write_stdin 等待同一次执行，不重复提交任务。

需要子任务时，检查当前工具菜单中的 spawn 参数。原生 SpawnInput 使用
 task、label、profile、run_in_background 和 retry_count。编程选择 scripting，
需要网络选择 general；子任务的授权范围不能超过父任务。

把任务文本写入私有临时文件，通过标准输入交给 codex exec，使用
--output-last-message 保存结果。具体参数以本机 CLI 帮助为准。
不把 API 密钥、用户配置或完整对话复制进公开任务文件。
完成后核对实际 diff 与验证结果。后台请求已接收不等于已经完成。
