"""记忆子系统的依赖注入缝(Phase 2)。

对照 Reference `agent/looping/ports.py:MemoryServices` —— runtime 只认识这个薄服务包,
不认识具体引擎实现,换引擎不再连锁改调用点。工厂对照 Reference `bootstrap/memory.py`:
memory 启用且配了 embedding 就用 DefaultMemoryEngine,否则退化成 DisabledMemoryEngine
(语义检索关闭,不发无谓的失败 embedding 请求;配好 [memory.embedding] 后自动切回)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kirakira_agent._compat.config_models import Config, build_config
from kirakira_agent._compat.net_http import SharedHttpResources
from kirakira_agent._compat.provider import LLMProvider
from kirakira_agent.coremem.default_engine import DefaultMemoryEngine
from kirakira_agent.coremem.default_memory_config import DefaultMemoryConfig
from kirakira_agent.coremem.engine import MemoryEngine
from kirakira_agent.coremem.plugin import DisabledMemoryEngine


@dataclass
class MemoryServices:
    """runtime 消费的记忆服务包。只暴露引擎接口,不暴露实现。"""

    engine: MemoryEngine | None = None


def memory_engine_enabled(config: Config) -> bool:
    """DefaultMemoryEngine 只在 memory 启用且配了 embedding 端点时启用。"""
    return bool(config.memory.enabled and config.memory.embedding.base_url)


def build_memory_engine(
    *,
    app_config: dict[str, Any],
    workspace: Path,
    provider: LLMProvider,
    light_provider: LLMProvider | None = None,
    http_resources: SharedHttpResources | None = None,
    event_publisher: Any = None,
) -> MemoryEngine:
    """构造记忆引擎:配了 embedding → DefaultMemoryEngine,否则 DisabledMemoryEngine。"""
    config = build_config(app_config)
    if not memory_engine_enabled(config):
        return DisabledMemoryEngine()
    return DefaultMemoryEngine(
        config=config,
        default_config=DefaultMemoryConfig(),
        workspace=workspace,
        provider=provider,
        light_provider=light_provider,
        http_resources=http_resources or SharedHttpResources(),
        event_publisher=event_publisher,
    )


def build_memory_services(
    *,
    app_config: dict[str, Any],
    workspace: Path,
    provider: LLMProvider,
    light_provider: LLMProvider | None = None,
    http_resources: SharedHttpResources | None = None,
    event_publisher: Any = None,
) -> MemoryServices:
    engine = build_memory_engine(
        app_config=app_config,
        workspace=workspace,
        provider=provider,
        light_provider=light_provider,
        http_resources=http_resources,
        event_publisher=event_publisher,
    )
    return MemoryServices(engine=engine)
