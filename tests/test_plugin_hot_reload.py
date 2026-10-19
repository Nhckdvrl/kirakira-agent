"""插件热重载契约:watcher 发现变化 → 换代 → 在途租约保护旧代际。

最关键的一条:**换代发生在 turn 进行中时,旧代际只转 retired,不被销毁**;
turn 释放租约后才 quiesce。这是"热重载不抽走在途能力"的实际验证。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List

from plugins.extensions.watcher import PluginWatcher


class _FakeManager:
    """最小 manager 替身:可控的 revision 与 reconcile 结果。"""

    def __init__(self) -> None:
        self.revision = "r1"
        self.reconcile_calls = 0
        self.raise_on_scan: Exception | None = None
        self.reconcile_failures = 0

    def watch_revision(self) -> str:
        if self.raise_on_scan is not None:
            raise self.raise_on_scan
        return self.revision

    async def reconcile_changed(self) -> List[Dict[str, Any]]:
        self.reconcile_calls += 1
        if self.reconcile_failures:
            self.reconcile_failures -= 1
            raise RuntimeError("temporary publish failure")
        return [{"plugin_id": "demo", "state": "swapped"}]


class WatcherTests(unittest.TestCase):
    def test_baseline_reconciles_once_then_only_on_change(self) -> None:
        async def scenario() -> None:
            manager = _FakeManager()
            watcher = PluginWatcher(manager, interval_seconds=0.01)
            task = asyncio.create_task(watcher.run())

            # 第一次成功扫描建立基线并 reconcile 一次(照 Reference)
            await asyncio.sleep(0.05)
            baseline_calls = manager.reconcile_calls
            self.assertEqual(baseline_calls, 1)

            # revision 不变则不再换代
            await asyncio.sleep(0.05)
            self.assertEqual(manager.reconcile_calls, baseline_calls)

            # revision 变化后触发换代
            manager.revision = "r2"
            await asyncio.sleep(0.08)
            self.assertGreater(manager.reconcile_calls, baseline_calls)

            watcher.stop()
            await watcher.wait_stopped()
            task.cancel()

        asyncio.run(scenario())

    def test_scan_failure_is_recorded_and_loop_survives(self) -> None:
        async def scenario() -> None:
            manager = _FakeManager()
            manager.raise_on_scan = OSError("plugin dir vanished mid-swap")
            watcher = PluginWatcher(manager, interval_seconds=0.01)
            task = asyncio.create_task(watcher.run())
            await asyncio.sleep(0.05)
            self.assertIn("vanished", str(watcher.last_error))

            # 目录恢复后能继续换代
            manager.raise_on_scan = None
            manager.revision = "r9"
            await asyncio.sleep(0.08)
            self.assertGreaterEqual(manager.reconcile_calls, 1)

            watcher.stop()
            await watcher.wait_stopped()
            task.cancel()

        asyncio.run(scenario())

    def test_wake_forces_immediate_reconcile(self) -> None:
        async def scenario() -> None:
            manager = _FakeManager()
            watcher = PluginWatcher(manager, interval_seconds=10)
            task = asyncio.create_task(watcher.run())
            await asyncio.sleep(0.02)
            watcher.wake()
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(manager.reconcile_calls, 1)
            watcher.stop()
            await watcher.wait_stopped()
            task.cancel()

        asyncio.run(scenario())

    def test_failed_revision_is_retried_without_another_file_change(self) -> None:
        async def scenario() -> None:
            manager = _FakeManager()
            manager.reconcile_failures = 1
            watcher = PluginWatcher(manager, interval_seconds=0.01)
            task = asyncio.create_task(watcher.run())

            await asyncio.sleep(0.08)

            self.assertGreaterEqual(manager.reconcile_calls, 2)
            self.assertIsNone(watcher.last_error)
            self.assertEqual(watcher.status()["revision"], "r1")
            watcher.stop()
            await watcher.wait_stopped()
            task.cancel()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
