"""Stage 4/5:显式记忆工具与主动兴趣检索走引擎。

引擎承重(配了 embedding)时:
- memorize/recall_memory/forget_memory → engine.mutate / engine.query(intent="answer")
- 主动 content 判断前做 engine.query(intent="interest", effect="read_only", floor="strong")
引擎未承重时全部回退旧路径,链路不中断。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from kirakira_agent.coremem.engine import (
    EngineProfile,
    MemoryCapability,
    MemoryEngineDescriptor,
    MemoryMutationResult,
    MemoryQueryResult,
    MemoryRecord,
)
from kirakira_agent.coremem.plugin import DisabledMemoryEngine
from kirakira_agent.coremem.services import MemoryServices
from kirakira_agent.tools.builtins import WorkspaceTools


_LIVE_DESCRIPTOR = MemoryEngineDescriptor(
    name="fake-live",
    profile=EngineProfile.RICH_MEMORY_ENGINE,
    capabilities=frozenset({MemoryCapability.RETRIEVE_CONTEXT_BLOCK}),
)


class _FakeEngine:
    DESCRIPTOR = _LIVE_DESCRIPTOR

    def __init__(self) -> None:
        self.mutate = AsyncMock()
        self.query = AsyncMock()


def _tools(engine: Any) -> WorkspaceTools:
    services = MemoryServices(engine=engine) if engine is not None else None
    return WorkspaceTools(
        __import__("pathlib").Path("."),
        SimpleNamespace(descriptions=lambda: ""),
        None,  # 旧 MemoryRuntime 不提供,证明确实走引擎
        None,
        None,
        None,
        memory_services=services,
    )


class MemoryToolsViaEngineTests(unittest.TestCase):
    def test_memorize_routes_to_engine_mutate(self) -> None:
        async def scenario() -> None:
            engine = _FakeEngine()
            engine.mutate.return_value = MemoryMutationResult(
                accepted=True, item_id="m1", actual_kind="preference", status="new"
            )
            out = await _tools(engine).memorize("以后用中文回复", memory_type="preference")
            self.assertIn("m1", out)
            engine.mutate.assert_awaited_once()
            request = engine.mutate.await_args.args[0]
            self.assertEqual(request.kind, "remember")
            self.assertEqual(request.summary, "以后用中文回复")
            self.assertEqual(request.memory_kind, "preference")

        asyncio.run(scenario())

    def test_recall_routes_to_engine_answer_query(self) -> None:
        async def scenario() -> None:
            engine = _FakeEngine()
            engine.query.return_value = MemoryQueryResult(
                records=[
                    MemoryRecord(
                        id="r1",
                        kind="preference",
                        summary="用户偏好中文",
                        score=0.91,
                        engine_kind="fake-live",
                    )
                ]
            )
            out = await _tools(engine).recall_memory("语言偏好", limit=3)
            payload = json.loads(out)
            self.assertEqual(payload[0]["id"], "r1")
            self.assertEqual(payload[0]["memory_type"], "preference")
            request = engine.query.await_args.args[0]
            self.assertEqual(request.intent, "answer")
            self.assertEqual(request.limit, 3)

        asyncio.run(scenario())

    def test_forget_routes_to_engine_and_reports_missing(self) -> None:
        async def scenario() -> None:
            engine = _FakeEngine()
            engine.mutate.return_value = MemoryMutationResult(
                accepted=True,
                status="superseded",
                affected_ids=["a"],
                missing_ids=["b"],
            )
            out = await _tools(engine).forget_memory(["a", "b"])
            payload = json.loads(out)
            self.assertEqual(payload["superseded_ids"], ["a"])
            self.assertEqual(payload["missing_ids"], ["b"])
            self.assertEqual(engine.mutate.await_args.args[0].kind, "forget")

        asyncio.run(scenario())

    def test_disabled_engine_falls_back_to_legacy_path(self) -> None:
        async def scenario() -> None:
            # DisabledMemoryEngine 能力集为空 → 不当作承重引擎;旧 runtime 也没有 → 明确报错
            tools = _tools(DisabledMemoryEngine())
            self.assertIsNone(tools._live_memory_engine())
            self.assertIn("not enabled", await tools.memorize("x"))
            self.assertEqual(await tools.recall_memory("x"), "[]")

        asyncio.run(scenario())

    def test_no_services_falls_back(self) -> None:
        async def scenario() -> None:
            tools = _tools(None)
            self.assertIsNone(tools._live_memory_engine())

        asyncio.run(scenario())


class ProactiveInterestTests(unittest.TestCase):
    def _loop(self, engine: Any):
        from kirakira_agent.proactive.loop import ProactiveLoop

        loop = ProactiveLoop.__new__(ProactiveLoop)
        loop._memory_services = MemoryServices(engine=engine) if engine else None
        return loop

    def test_interest_query_uses_readonly_strong_floor(self) -> None:
        async def scenario() -> None:
            engine = _FakeEngine()
            engine.query.return_value = MemoryQueryResult(
                records=[
                    MemoryRecord(
                        id="i1",
                        kind="preference",
                        summary="关注 AI benchmark 结果",
                        score=0.8,
                        engine_kind="fake-live",
                    )
                ]
            )
            text = await self._loop(engine)._interest_hits("新的 benchmark 发布")
            self.assertIn("benchmark", text)
            request = engine.query.await_args.args[0]
            self.assertEqual(request.intent, "interest")
            self.assertEqual(request.effect, "read_only")
            self.assertEqual(request.filters.relevance_floor, "strong")

        asyncio.run(scenario())

    def test_interest_empty_when_engine_not_load_bearing(self) -> None:
        async def scenario() -> None:
            self.assertEqual(
                await self._loop(DisabledMemoryEngine())._interest_hits("x"), ""
            )
            self.assertEqual(await self._loop(None)._interest_hits("x"), "")

        asyncio.run(scenario())

    def test_interest_failure_does_not_break_tick(self) -> None:
        async def scenario() -> None:
            engine = _FakeEngine()
            engine.query.side_effect = RuntimeError("embedding down")
            self.assertEqual(await self._loop(engine)._interest_hits("x"), "")

        asyncio.run(scenario())

    def test_blank_query_skips_retrieval(self) -> None:
        async def scenario() -> None:
            engine = _FakeEngine()
            self.assertEqual(await self._loop(engine)._interest_hits("   "), "")
            engine.query.assert_not_awaited()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
