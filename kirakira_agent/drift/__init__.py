"""Drift 空闲任务链路。

主动链路（proactive）拉了一圈三通道都没东西可推时，agent 不空转，而是进入
Drift 模式：读用户写的 ``SKILL.md``（分步操作指南）当 system prompt，注入一份
Drift Briefing（记忆 + 近期上下文 + 本 skill 连续性），一步步执行，最后调
``finish_drift`` 收尾。

参考 akashic 的 `plugins/drift_flow` + `plugins/wake_proactive/drift_drive.py`。
MVP 复用 kirakira 现有的 Agent loop 与工具，保留跨轮连续性（drift.db），
不搬 hazard 穿线 / self_observation journal 等 Tier-3 细节。
"""

from kirakira_agent.drift.runner import DriftRunner
from kirakira_agent.drift.skills import DriftSkill, discover_skills
from kirakira_agent.drift.state import DriftStateStore

__all__ = ["DriftRunner", "DriftSkill", "discover_skills", "DriftStateStore"]
