"""记忆子系统的依赖注入缝(Phase 2)。

对照 Reference `agent/looping/ports.py:MemoryServices` —— runtime 只认识这个薄服务包,
不认识具体引擎实现,换引擎不再连锁改调用点。工厂对照 Reference `bootstrap/memory.py`:
memory 启用且配了 embedding 就用 DefaultMemoryEngine,否则退化成 DisabledMemoryEngine
(语义检索关闭,不发无谓的失败 embedding 请求;配好 [memory.embedding] 后自动切回)。
"""

from __future__ import annotations

import inspect
import logging
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

logger = logging.getLogger(__name__)


@dataclass
class MemoryServices:
    """runtime 消费的记忆服务包。只暴露引擎接口,不暴露实现。

    `store` 是引擎拥有的 MemoryStore2。暴露它不是为了让业务代码绕过引擎,而是让
    Dashboard 与过渡期的旧 MemoryRuntime **共享同一个 SQLite 连接**——否则同一个
    coremem.db 会被打开两次,产生锁竞争与不一致视图。引擎未承重时为 None。
    """

    engine: MemoryEngine | None = None
    store: Any = None

    async def aclose(self) -> None:
        """关闭引擎持有的资源(store / embedder / 事件订阅)。

        照 Reference `core/memory/runtime.py:MemoryRuntime.aclose`:逆序释放,
        单个 closeable 失败不掩盖其余,首个异常在全部尝试后抛出。
        """
        closeables = list(getattr(self.engine, "closeables", []) or [])
        first_error: BaseException | None = None
        for closeable in reversed(closeables):
            try:
                if hasattr(closeable, "aclose"):
                    result = closeable.aclose()
                    if inspect.isawaitable(result):
                        await result
                elif hasattr(closeable, "close"):
                    closeable.close()
            except Exception as exc:  # noqa: BLE001 - 关停期不因单个资源中断
                if first_error is None:
                    first_error = exc
                logger.warning(
                    "memory closeable shutdown failed for %s: %s",
                    type(closeable).__name__,
                    exc,
                )
        if first_error is not None:
            raise first_error


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
    # 引擎是 coremem.db 的唯一 owner;把它的 store 一并暴露,过渡期消费者共享同一连接。
    return MemoryServices(engine=engine, store=getattr(engine, "_v2_store", None))
