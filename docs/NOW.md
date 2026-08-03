# 当前保留项

本文件只记录尚未完成或明确延期的事项。已完成内容见各专题文档与 Git 历史。

## 已明确延期

1. **Akasha v2**：当前继续使用并维护可工作的 Akasha v1。v2 的重建、恢复和更厚事务合同以后在
   `core/memory` plugin/engine/admin 边界内替换，不建立平行目录。
2. **前端与移动后端对齐**：本轮不复刻 Reference 的 React/Android/mobile realtime/WebUI OTA。
   现有 TUI、Web、dashboard 保持可用。
3. **完整 benchmark/运维体系**：当前已有 semantic contract、change-impact gate、离线全测与
   DeepSeek 在线验证；Docker/Harbor 私有 campaign、systemd/guardian 深度以后按需要补。

## 可加厚但不阻塞使用

- 主动链：日配额、累计 hazard、兴趣 embedding 与更细的数据源失败策略。
- Provider：更多 provider profile、reasoning effort 与设置 UI。
- 插件分发：非 git 源、版本缓存、独立 MCP venv。
- 轨迹 UI：后端已经保存普通 turn 的 `tool_chain/context_trace` 和主动 tick 的
  `tick_log/tick_step_log`；新可视化页面属于暂缓的前端范围。
- 渠道触发的自重启：控制面换代合同已存在，渠道投递与 restart commit 的完整联动仍可加厚。

这些项目的完整边界见 [DIFFERENCE_AUDIT.md](./DIFFERENCE_AUDIT.md)。任何新迁移都必须继续满足：
移走 `Reference/` 后，运行、构建、测试和 doctor 不受影响。
