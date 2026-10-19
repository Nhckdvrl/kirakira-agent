# 2026-10-19：项目与目录功能验证

本轮恢复以功能对齐为主线。上游范围固定为
[`21b38f0e102d`](https://github.com/kachofugetsu09/akashic-agent/tree/21b38f0e102d9e1bdc5576725635dce29f135c0c)，
已登记全部 70 个生产插件和 SDK、部署、客户端三个跨目录面，详见[功能清单](../../status/akashic-features.md)。
清单完整不等于功能全部完成；本轮实现并验证 Project/default/cwd 这条实际运行链。

## 实际覆盖

| 场景 | 验证入口与断言 |
| --- | --- |
| 项目生命周期 | 稳定创建回执、改名、归档、一次性绑定、并发绑定唯一成功；归档阻止新会话而不破坏旧会话 |
| 同库创建 | Session 和目录在同一事务内；后续 initializer 失败全部回滚；并发创建只发布一个缓存对象 |
| 删除与重建 | Session、目录及切换回执同事务删除；保留项目和其他会话，失败回滚；同一路由可按项目默认值重新创建 |
| 升级 | 旧会话不回填默认目录，原消息 ID/内容不变，新迁移加入 Yoyo 正式目录 |
| 切换 | 当前修订 CAS、显式 null 不绕过检查、回执重试、不同会话互不影响 |
| 丢失目录 | 相对路径失败且不重建目录；显式绝对目标仍可访问；状态显示 unavailable |
| 仓库规则 | 最近 Git 根、override 优先、实时删除、32 KiB 限制、整链失败不沿用旧正文 |
| 实际主链 | 混合目录切换批次全部拒绝；单独切换后下一模型请求只含新规则，相对读取命中新目录 |
| 上下文治理 | 关闭窗口和启用 100k 窗口两种路径，实时前缀同步进入 compactor，规则正文不写入原始工具链 |
| Shell/PTY | 真实 Shell pwd、真实 Git worktree；运行中 PTY 保持原 cwd，之后的文件写入落到新 cwd |
| 子任务 | 子会话创建时继承目录；子任务切换不改变父目录 |
| 外部切换 | 经过共享 admission 等待当前轮次，不能穿过正在执行的工具链 |
| 协议与产品入口 | 实际 Unix socket、HTTP 项目页面/控制 API、项目聊天写入代码目录；参数校验和跨源拒绝 |
| 重启 | 关闭并重新构造真实 runtime 后，项目默认值、Session cwd 与相对文件读取继续成立 |

## 离线回归

单一项目 `.venv` / Python 3.14.6。最终全量 **738 passed、4 subtests passed**（58.02 秒），22 条既有 Telegram 弃用警告。
新增的 17 项（含参数化）使用真实 SQLite、文件系统、Socket、HTTP 和进程组件；确定性模型只控制工具序列。
最后代码检查后的目录/控制专项为 **34 passed**。

首次全量为 **717 passed / 4 failed**。三项是迁移期望列表未加入新 ID，一项是迁移测试的
旧 `sessions.db` 只是 opaque 字节夹具。已补齐正式目录并在新迁移中识别 SQLite 文件头，
不改旧迁移，不重写历史库，不放松原消息断言；后续全量通过。

发布后再次复查发现删除会话会遗留新增目录行，导致同一路由重建时发生 owner CAS 冲突。
已补齐事务内 owner 删除钩子、目录/回执清理，以及删除失败整事务回滚；增补两项回归，
并在最后 Step 轮中实际删除、重建项目会话，再调用文件工具验证默认目录。

## Step5 真实功能

验证器直接装配生产 runtime，只授予目录、文件和 Shell/进程所需工具，在独立临时目录运行。
仅使用 `https://api.stepfun.com/step_plan/v1`、`step-5-preview`、原生 `reasoning_effort=medium`。
密钥从外部文件读取，不进入制品。记忆在此验证中禁用。

每轮包含目录切换、项目默认目录、重启恢复三类，各重复三次。断言包括真实工具效果、随机文件内容、
真实 journal 的下一请求规则替换和最终 completed 状态；模型仅说“完成”不能通过。

| 批次 | 场景通过 | 成功模型响应 | 返回 total_tokens |
| --- | --- | --- | --- |
| 首轮 | 9/9 | 33 | 65,795 |
| 代码复核轮 | 9/9 | 33 | 65,856 |
| 发布前轮 | 9/9 | 34 | 68,097 |
| 工具回执证据轮 | 9/9 | 33 | 67,393 |
| 删除/重建生命周期轮 | 9/9 | 40 | 79,712 |

共 45 次场景执行、173 个成功模型响应，返回 usage 合计 346,853 tokens，未观察到模型 I/O 错误。
这是三类场景的重复验证，不是 45 类不同功能。最后两轮额外保存真实 tool_result 和逐场景检查，
要求目录/读取/Shell（或项目写入/回读）实际成功；重启后也必须再次成功调用读取工具。
最终生产源码和验证器的摘要与该轮报告一致。此前结果全部保留。
这些是功能验证，不是整体性能评测或模型能力榜单。

## 工程检查与制品

全量清单与固定上游插件集合一致，本地落点、生成 Markdown、附加范围路径均通过检查，已加入 CI。
新增模块 Ruff F/I、修改的已有模块 Ruff F、Web 脚本 Node 语法检查、383 个本地 Markdown 链接检查通过。
既有迁移未改变，10 个 Akasha2 固定算法文件仍逐字节匹配 pin。
sdist/wheel 构建和 CLI 帮助通过；核对 28 个本轮代码模块与制品一致，sdist 的两份修改测试也与源码一致，
制品没有运行时数据库。
没有声称执行了全项目类型检查或真实浏览器 UI 测试。

| 原始文件 | 内容 |
| --- | --- |
| `pytest-initial-failures.txt` / `pytest-final.txt` | 首次迁移失败和删除补漏前的 736 项结果 |
| `pytest-lifecycle.txt` / `pytest-workspace-lifecycle.txt` | 最终 738 项全量及补充 Web 删除后重建的 34 项专项 |
| `pytest-workspace-control.txt` | 删除补漏前的目录与控制专项 |
| `step-first.json` / `step-review.json` / `step-release.json` | 前三轮逐场景及原生 usage |
| `step-evidence.json` / `step-lifecycle.json` / `step-summary.json` | 生产工具回执、规则与生命周期检查、源码摘要和跨轮汇总 |
| `feature-catalog.json` / `change-impact.json` | 全量范围及变更影响选择 |
| `package-check.json` / `verification.json` | 制品校验与最终源码/结果摘要 |

失败日志仅去除行尾空白，保留全部失败正文；manifest 同时记录原日志摘要及此转换，便于核对。

## 复现

```bash
uv run --frozen python -m scripts.check_feature_alignment --reference /path/to/akashic-agent
uv run --frozen pytest -q tests/test_workspace_features.py tests/test_control_plane.py
uv run --frozen pytest -q
uv run --frozen python -m eval.scenarios.workspace \
  --key-file /path/to/private-step-plan.key \
  --workspace /tmp/kirakira-workspace-functional-new \
  --output /tmp/kirakira-workspace-functional-new.json \
  --repeats 3 --concurrency 2
```

workspace 和 output 必须为新的测试路径。原始结果存放于
[`eval/results/2026-10-19-workspace-functions`](../../../eval/results/2026-10-19-workspace-functions)。

## 证据边界

已覆盖本地 Project/cwd 的生产接线和故障语义。未做真实浏览器的焦点/响应式交互验收，
未移植上游 HostBridge、prepared 工具恢复、完整工作台或移动端；也没有把项目当作记忆/租户边界。
当前全部 70 项仍按 partial/missing 保守登记，后续要逐项达到完整合同再改为 aligned。
用户真实会话、记忆库和私人配置未用于这些验证。
