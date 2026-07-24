"""consolidation 驱动权移交契约(NOW.md 第 1 项 / decisions/0002 的后继)。

移交后必须同时成立:
- 有承重维护器时,runtime 不再调旧 schedule_consolidation/consolidate_turn(不重复归档);
- context guard 仍然有效:超阈值且归档无法推进时拒绝本轮,不静默丢历史;
- 没有维护器时(未配 embedding / 未绑定 session)回退旧路径,链路不中断。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from kirakira_agent.coremem.services import MemoryServices
from kirakira_agent.runtime import PassiveTurnPipeline, RuntimeConfig


class _Session:
    def __init__(self, count: int, consolidated: int = 0) -> None:
        self.key = "cli:1"
        self.messages = [{"role": "user", "content": "m%d" % i} for i in range(count)]
        self.last_consolidated = consolidated
        self.metadata: dict[str, Any] = {"channel": "cli", "chat_id": "1"}


class _Maintenance:
    """替身维护器:可控是否推进 last_consolidated。"""

    def __init__(self, *, advances: bool = True, bound: bool = True) -> None:
        self._get_session = (lambda key: None) if bound else None
        self.advances = advances
        self.calls: list[Any] = []

    async def consolidate(self, request):
        self.calls.append(request)
        if self.advances:
            request.session.last_consolidated = len(request.session.messages)
        return SimpleNamespace(consolidated_count=1, trace={"mode": "markdown"})


def _pipeline(maintenance: Any) -> PassiveTurnPipeline:
    pipeline = PassiveTurnPipeline.__new__(PassiveTurnPipeline)
    pipeline.config = RuntimeConfig(model="m", history_window=10)
    pipeline.memory_services = (
        MemoryServices(markdown=SimpleNamespace(maintenance=maintenance))
        if maintenance is not None
        else None
    )
    saved: list[Any] = []

    async def save_async(session):
        saved.append(session)

    pipeline.session_manager = SimpleNamespace(save_async=save_async)
    pipeline.saved = saved  # 便于断言
    return pipeline


class MaintenanceDetectionTests(unittest.TestCase):
    def test_bound_maintenance_is_used(self) -> None:
        pipeline = _pipeline(_Maintenance(bound=True))
        self.assertIsNotNone(pipeline._markdown_maintenance())

    def test_unbound_maintenance_falls_back(self) -> None:
        # 没绑定 session 生命周期的维护器不能驱动归档
        pipeline = _pipeline(_Maintenance(bound=False))
        self.assertIsNone(pipeline._markdown_maintenance())

    def test_no_services_falls_back(self) -> None:
        self.assertIsNone(_pipeline(None)._markdown_maintenance())


class ContextGuardTests(unittest.TestCase):
    def test_below_threshold_passes_without_consolidating(self) -> None:
        async def scenario() -> None:
            maintenance = _Maintenance()
            pipeline = _pipeline(maintenance)
            # history_window=10 → threshold = 10 + max(5,5) = 15
            session = _Session(count=5)
            self.assertEqual(
                await pipeline._guard_memory_context(session, "cli:1"), ""
            )
            self.assertEqual(maintenance.calls, [])

        asyncio.run(scenario())

    def test_over_threshold_consolidates_and_passes(self) -> None:
        async def scenario() -> None:
            maintenance = _Maintenance(advances=True)
            pipeline = _pipeline(maintenance)
            session = _Session(count=40)
            self.assertEqual(
                await pipeline._guard_memory_context(session, "cli:1"), ""
            )
            # 强制归档,且推进后保存了 session
            self.assertEqual(len(maintenance.calls), 1)
            self.assertTrue(maintenance.calls[0].force)
            self.assertEqual(len(pipeline.saved), 1)

        asyncio.run(scenario())

    def test_stalled_consolidation_refuses_the_turn(self) -> None:
        async def scenario() -> None:
            # 归档没有推进 → 必须拒绝本轮,避免静默丢历史
            maintenance = _Maintenance(advances=False)
            pipeline = _pipeline(maintenance)
            reply = await pipeline._guard_memory_context(_Session(count=40), "cli:1")
            self.assertIn("安全阈值", reply)
            self.assertEqual(pipeline.saved, [])

        asyncio.run(scenario())

    def test_consolidate_exception_refuses_instead_of_crashing(self) -> None:
        async def scenario() -> None:
            class _Boom(_Maintenance):
                async def consolidate(self, request):
                    raise RuntimeError("consolidation exploded")

            pipeline = _pipeline(_Boom())
            reply = await pipeline._guard_memory_context(_Session(count=40), "cli:1")
            # 异常不外抛,但也不能放行——仍按"未能推进"处理
            self.assertIn("安全阈值", reply)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
