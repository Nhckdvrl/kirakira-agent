# 维护与诊断

此目录保留日常维护、恢复和诊断工具。可复用评测统一放在 [`eval/`](../eval/README.md)。

| 命令模块 | 用途 |
| --- | --- |
| `scripts.check_upstream_kernel` | 校验原生内核与 Akasha2 固定文件 |
| `scripts.check_feature_alignment` | 校验功能清单，`--write` 更新对应文档 |
| `scripts.check_yoyo_migrations` | 检查已注册迁移不可变 |
| `scripts.check_change_impact` | 选择受影响合同，`--run` 执行 |
| `scripts.inspect_execution` | 只读查看执行事实和未知结果 |
| `scripts.replay_akasha` / `scripts.replay_akasha2` | 从来源库显式重建到新的目标库 |
| `scripts.verify_online` | 在隔离目录验证已配置 provider；通过 `--help` 查看要求 |
| `scripts.clean_workspace` | 删除生成的缓存、构建产物、字节码及空目录 |

```bash
uv run --frozen python -B -m scripts.clean_workspace --dry-run
uv run --frozen python -B -m scripts.clean_workspace
```

清理器跳过 Git 已跟踪文件，不跟随符号链接，保留唯一 `.venv`、Reference 和运行数据目录。
私有配置、数据库、锁文件、模型缓存和评测记录不属于清理对象。
pytest 与 Ruff 的新缓存统一写入已忽略的 `.cache/`；Python 字节码可用 `-B` 避免生成。

临时修补、迁移辅助和结果整理的一次性脚本不进入仓库。需要保留的复现程序应归入对应模块，
运行输出写入新的隔离目录；被引用的历史结果不能当缓存删除。
