"""主动推送链路（Proactive）。

被动链路负责"用户问、agent 答"；这个包负责"agent 自己找你"：
按电量模型自适应轮询三路数据（alert / content / context），由 LLM 判断
是否推送。没有可推的东西时把控制权交给 Drift 链路（``kirakira_agent.drift``）。

移植自 akashic-agent 参考实现（`proactive_v2` + `plugins/wake_proactive`），
MVP 只保留差异化本质：电量自适应调度 + 三通道语义 + 可插拔数据源，
不搬 phase-graph kernel / snapshot 热重载 / 语义兴趣向量等 Tier-3 机制。
"""

from kirakira_agent.proactive.config import DriftConfig, ProactiveConfig
from kirakira_agent.proactive.loop import ProactiveLoop
from kirakira_agent.proactive.sources import (
    FileInboxSource,
    ProactiveSource,
    SourceRegistry,
)

__all__ = [
    "DriftConfig",
    "ProactiveConfig",
    "ProactiveLoop",
    "ProactiveSource",
    "SourceRegistry",
    "FileInboxSource",
]
