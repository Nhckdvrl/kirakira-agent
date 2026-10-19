"""主动链路 lifecycle 化(照 Reference proactive_v2/lifecycle.py + drift_flow/modules.py)。

原来 `_tick()` 是扁平顺序链,步骤之间靠代码行序耦合。现在每步是一个声明
slot/requires/produces 的模块,顺序由依赖图决定,插件可以插进中间。

两条最关键的语义:
- 顺序由 requires 决定,**不是注册行序**;
- 扁平链里的 `return` 变成 `frame.finish(reason)`,后续模块看到 done 就跳过。
"""

from __future__ import annotations

import asyncio
import random
import unittest
from datetime import datetime, timezone

from agent.lifecycle.phase import topo_sort_modules
from plugins.proactive_flow.modules import (
    DriftModule,
    _LoopModule,
    build_default_proactive_modules,
)
from proactive_v2.frame import (
    SLOT_PROPOSAL_DRIFT,
    ProactiveFrame,
    new_proactive_frame,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


class FrameTests(unittest.TestCase):
    def test_finish_records_the_first_reason_only(self) -> None:
        frame = new_proactive_frame("web:u1", now=NOW)
        frame.finish("alert_pushed").finish("drift")
        # 第一个原因胜出:后面的模块不该覆盖"本轮为什么结束"
        self.assertEqual(frame.terminal, "alert_pushed")
        self.assertTrue(frame.done)


class ModuleOrderTests(unittest.TestCase):
    def _slots(self, modules) -> list[str]:
        return [m.slot for m in topo_sort_modules(modules)]

    def test_default_pipeline_orders_by_dependency(self) -> None:
        order = self._slots(build_default_proactive_modules(loop=None))
        self.assertEqual(
            order,
            [
                "proactive.gate",
                "proactive.fetch",
                "proactive.ingest",
                "proactive.judge_context",
                "proactive.alert",
                "proactive.content",
                "proactive.drift",
            ],
        )

    def test_registration_order_does_not_change_execution_order(self) -> None:
        modules = build_default_proactive_modules(loop=None)
        shuffled = modules[:]
        random.Random(7).shuffle(shuffled)
        self.assertEqual(self._slots(shuffled), self._slots(modules))

    def test_plugin_module_lands_where_its_requires_say(self) -> None:
        class _PluginModule(_LoopModule):
            slot = "plug.after_ingest"
            requires = ("proactive.ingest",)

            async def execute(self, frame):
                return frame

        modules = build_default_proactive_modules(loop=None) + [_PluginModule(None)]
        order = self._slots(modules)
        # 插件模块必须排在它依赖的 ingest 之后
        self.assertGreater(
            order.index("plug.after_ingest"), order.index("proactive.ingest")
        )


class TerminalShortCircuitTests(unittest.TestCase):
    def test_finished_frame_skips_remaining_modules(self) -> None:
        async def scenario() -> None:
            ran: list[str] = []

            class _Recorder(_LoopModule):
                def __init__(self, slot: str) -> None:
                    super().__init__(None)
                    self.slot = slot

                async def execute(self, frame: ProactiveFrame) -> ProactiveFrame:
                    ran.append(self.slot)
                    return frame

            class _Stopper(_Recorder):
                async def execute(self, frame: ProactiveFrame) -> ProactiveFrame:
                    ran.append(self.slot)
                    return frame.finish("stopped")

            frame = new_proactive_frame("web:u1", now=NOW)
            for module in (
                _Recorder("a.first"),
                _Stopper("a.stop"),
                _Recorder("a.last"),
            ):
                frame = await module.run(frame)

            self.assertEqual(ran, ["a.first", "a.stop"])
            self.assertEqual(frame.terminal, "stopped")

        asyncio.run(scenario())


class DriftModuleTests(unittest.TestCase):
    """Drift 现在是流水线上的模块,而不是 runtime 里的一个 hook 调用。"""

    class _Loop:
        def __init__(self, drifted: bool) -> None:
            self.decisions: list[tuple] = []
            self._drift_hook = self._hook if drifted is not None else None
            self._drifted = drifted
            self._state = self

        async def _hook(self, now, session_key) -> bool:
            return self._drifted

        def record_decision(self, now, action, detail="") -> None:
            self.decisions.append((action, detail))

    def test_drift_module_records_and_finishes(self) -> None:
        async def scenario() -> None:
            for drifted, expected in ((True, "drifted"), (False, None)):
                loop = self._Loop(drifted)
                frame = await DriftModule(loop).run(new_proactive_frame("s", now=NOW))
                self.assertEqual(frame.get(SLOT_PROPOSAL_DRIFT), expected)
                self.assertEqual(loop.decisions[0][0], "drift" if drifted else "idle")
                self.assertTrue(frame.done)

        asyncio.run(scenario())


class ProactiveAssemblyTests(unittest.TestCase):
    """Invalid slot declarations fail during assembly."""

    def _minimal_loop(self):
        from proactive_v2.loop import ProactiveLoop

        loop = ProactiveLoop.__new__(ProactiveLoop)
        loop._tick_lock = asyncio.Lock()
        return loop

    def test_assembly_error_fails_loud_at_add_time_not_in_tick(self) -> None:
        loop = self._minimal_loop()
        loop._modules = topo_sort_modules(build_default_proactive_modules(loop))

        class _Duplicate(_LoopModule):
            slot = "proactive.gate"  # 与内置 gate 撞 slot

        with self.assertRaises(RuntimeError):
            from proactive_v2.loop import ProactiveLoop

            ProactiveLoop.add_modules(loop, [_Duplicate(None)])
        # 坏声明只影响它自己的注册操作,已编译流水线保持原样
        self.assertEqual(len(loop._modules), 7)


if __name__ == "__main__":
    unittest.main()
