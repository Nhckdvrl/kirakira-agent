"""Public memory capability values, independent of the selected implementation."""

import inspect
import logging
from dataclasses import dataclass
from typing import Any

from agent.composition import ServiceKey
from core.memory.engine import MemoryEngine

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
    # 四文件 Markdown 长期档案 + consolidation 维护(Reference 里与 engine 并列)。
    markdown: Any = None

    async def run_maintenance(self) -> None:
        """Optional engine-owned recovery loop; runtime owns its task lifetime."""
        maintain = getattr(self.engine, "run_maintenance", None)
        if callable(maintain):
            await maintain()

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


MEMORY_SERVICES = ServiceKey[MemoryServices]("memory.services.v1")


class MemoryReader:
    """Plugins receive retrieval, with no mutation or store interface."""

    def __init__(self, engine):
        self._engine = engine

    async def query(self, request):
        return await self._engine.query(request)

    def describe(self):
        return self._engine.describe()


MEMORY_READER = ServiceKey[MemoryReader]("memory.retrieval.v1")

from agent.composition import ServiceKey

MEMORY = ServiceKey("memory.profile.v1")
RECALL_INSPECTOR = ServiceKey("memory.recall_inspector.v1")
