"""Dashboard 数据面(对照 Reference `bootstrap/dashboard_api.py`)。

三条最关键的语义:
- 记忆走引擎的 admin 协议,**不再绕过它直接摸 store**(换引擎时 Dashboard 不该坏掉);
- 各依赖缺席时对应面板返回空而不是抛异常(最小构造/gateway 调试也能打开页面);
- 状态库读被 marshal 回属主线程——proactive.db/drift.db 的连接有线程亲和,
  而 Web 的 HTTP handler 跑在另一个线程里。
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from kirakira_agent.dashboard import DashboardService


class _FakeAdminEngine:
    """只实现 admin 协议的假引擎;有 list_items_for_dashboard 才算承重。"""

    DESCRIPTOR = SimpleNamespace(
        name="fake", profile=SimpleNamespace(value="rich"), capabilities=frozenset({"retrieve.semantic"})
    )

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_items_for_dashboard(self, **kwargs):
        self.calls.append("list")
        self.seen = kwargs
        return ([{"id": "m1", "summary": "hi", "status": "active"}], 1)

    def get_item_for_dashboard(self, item_id, **_):
        self.calls.append("get")
        return {"id": item_id, "summary": "hi"}

    def find_similar_items_for_dashboard(self, item_id, *, top_k=8, **_):
        self.calls.append("similar")
        return [{"id": "m2", "score": 0.9}]

    def delete_items_batch(self, ids):
        self.calls.append("delete")
        return len(ids)

    def tool_profile(self):
        return SimpleNamespace(
            recall=SimpleNamespace(name=""),
            memorize=SimpleNamespace(name=""),
            forget=SimpleNamespace(name=""),
            tools=(),
        )


class _LegacyOnlyMemory:
    """旧 MemoryRuntime:引擎未承重时的回退路径。"""

    engine = "coremem"
    store2 = None

    def __init__(self) -> None:
        self.records = [{"id": "old1", "content": "legacy"}]

    def list_records(self, include_forgotten=False):
        return list(self.records)


class MemoryGoesThroughEngineTests(unittest.TestCase):
    def test_list_detail_similar_delete_all_route_to_engine(self) -> None:
        engine = _FakeAdminEngine()
        board = DashboardService(
            workspace=Path("."),
            memory_services=SimpleNamespace(engine=engine),
            memory=_LegacyOnlyMemory(),
        )
        result = board.memories({"q": ["hi"], "page_size": ["10"]})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["engine"], "fake")
        self.assertEqual(board.memory_item("m1")["id"], "m1")
        self.assertEqual(board.memory_similar("m1")[0]["id"], "m2")
        self.assertEqual(board.delete_memories(["m1"], confirm="HARD_DELETE"), 1)
        # 关键:四条路径全部落到引擎,一次也没绕过它去摸旧 store
        self.assertEqual(engine.calls, ["list", "get", "similar", "delete"])

    def test_filters_and_paging_reach_engine(self) -> None:
        engine = _FakeAdminEngine()
        board = DashboardService(workspace=Path("."), memory_services=SimpleNamespace(engine=engine))
        board.memories(
            {
                "q": ["部署"],
                "memory_type": ["preference"],
                "status": ["active"],
                "has_embedding": ["true"],
                "page": ["3"],
                "page_size": ["999"],
            }
        )
        self.assertEqual(engine.seen["q"], "部署")
        self.assertEqual(engine.seen["memory_type"], "preference")
        self.assertIs(engine.seen["has_embedding"], True)
        self.assertEqual(engine.seen["page"], 3)
        self.assertEqual(engine.seen["page_size"], 200)  # 钳制到上限

    def test_absent_has_embedding_does_not_filter(self) -> None:
        engine = _FakeAdminEngine()
        board = DashboardService(workspace=Path("."), memory_services=SimpleNamespace(engine=engine))
        board.memories({})
        self.assertIsNone(engine.seen["has_embedding"])

    def test_hard_delete_requires_confirmation(self) -> None:
        board = DashboardService(
            workspace=Path("."), memory_services=SimpleNamespace(engine=_FakeAdminEngine())
        )
        with self.assertRaises(ValueError):
            board.delete_memories(["m1"], confirm="")

    def test_falls_back_to_legacy_when_engine_not_load_bearing(self) -> None:
        # DisabledMemoryEngine 没有 admin 方法 → 不算承重,回退旧栈
        board = DashboardService(
            workspace=Path("."),
            memory_services=SimpleNamespace(engine=object()),
            memory=_LegacyOnlyMemory(),
        )
        result = board.memories({})
        self.assertEqual(result["engine"], "legacy")
        self.assertEqual(result["memories"][0]["id"], "old1")

    def test_engine_info_reports_load_bearing(self) -> None:
        live = DashboardService(
            workspace=Path("."), memory_services=SimpleNamespace(engine=_FakeAdminEngine())
        )
        self.assertTrue(live.engine_info()["load_bearing"])
        self.assertEqual(live.engine_info()["capabilities"], ["retrieve.semantic"])
        bare = DashboardService(workspace=Path("."))
        self.assertFalse(bare.engine_info()["load_bearing"])


class DegradesWithoutDependenciesTests(unittest.TestCase):
    """最小构造下每个面板都要能打开,不能抛。"""

    def test_all_panels_return_empty_shapes(self) -> None:
        board = DashboardService(workspace=Path("."))
        self.assertEqual(board.sessions(), [])
        self.assertIsNone(board.session("nope"))
        self.assertFalse(board.delete_session("nope"))
        self.assertEqual(board.plugins()["active"], [])
        self.assertEqual(board.proactive(), {"enabled": False})
        self.assertEqual(board.drift(), {"enabled": False})
        self.assertEqual(board.memories({})["total"], 0)
        self.assertIsNone(board.memory_item("x"))
        self.assertEqual(board.memory_similar("x"), [])

    def test_overview_survives_all_absent(self) -> None:
        overview = DashboardService(workspace=Path("/tmp/ws")).overview()
        self.assertFalse(overview["memory"]["load_bearing"])
        self.assertFalse(overview["proactive"]["enabled"])
        self.assertFalse(overview["restart"]["supervised"])

    def test_failing_subsystem_is_reported_not_raised(self) -> None:
        class _Boom:
            def status(self):
                raise RuntimeError("db closed")

        board = DashboardService(workspace=Path("."), proactive_loop=_Boom())
        payload = board.proactive()
        self.assertTrue(payload["enabled"])
        self.assertIn("db closed", payload["error"])


class ThreadAffinityTests(unittest.TestCase):
    """状态库读必须回到属主线程执行——这是实弹跑出来的 bug(HTTP handler 另起线程)。"""

    def test_read_is_marshalled_to_owner_loop_thread(self) -> None:
        result: dict[str, int] = {}

        async def scenario() -> None:
            owner_thread = threading.get_ident()
            board = DashboardService(
                workspace=Path("."),
                loop=asyncio.get_running_loop(),
                proactive_loop=SimpleNamespace(
                    status=lambda: {"ran_on": threading.get_ident()}, _modules=[]
                ),
            )

            def from_other_thread() -> None:
                result["value"] = board.proactive()["ran_on"]

            worker = threading.Thread(target=from_other_thread)
            worker.start()
            while worker.is_alive():
                await asyncio.sleep(0.01)
            worker.join()
            result["owner"] = owner_thread

        asyncio.run(scenario())
        self.assertEqual(result["value"], result["owner"])

    def test_without_loop_runs_inline(self) -> None:
        board = DashboardService(
            workspace=Path("."),
            proactive_loop=SimpleNamespace(status=lambda: {"ok": True}, _modules=[]),
        )
        self.assertTrue(board.proactive()["ok"])


class SessionPanelTests(unittest.TestCase):
    def test_session_detail_clips_and_reports_cursor(self) -> None:
        class _Session:
            key = "web:u1"
            metadata = {"channel": "web"}
            last_consolidated = 2
            messages = [
                {"role": "user", "content": "x" * 20, "timestamp": "t%d" % i}
                for i in range(10)
            ]

        board = DashboardService(
            workspace=Path("."),
            session_manager=SimpleNamespace(
                get_or_create=lambda key: _Session(),
                list_sessions=lambda: [{"key": "web:u1", "message_count": 10}],
            ),
        )
        detail = board.session("web:u1", limit=3)
        self.assertEqual(detail["total_messages"], 10)
        self.assertEqual(len(detail["messages"]), 3)  # 只回最近 3 条
        self.assertEqual(detail["last_consolidated"], 2)


class PluginPanelTests(unittest.TestCase):
    def test_generation_lease_counts_are_surfaced(self) -> None:
        manager = SimpleNamespace(
            active=[SimpleNamespace(plugin_id="p1", version="1.0", desc="d", root="/r", instance=object())],
            errors={"bad": "boom"},
            generations=SimpleNamespace(
                active=(
                    SimpleNamespace(
                        plugin_id="p1", generation_id="g1", revision="r",
                        state="active", lease_count=2,
                    ),
                ),
                retired=(
                    SimpleNamespace(
                        plugin_id="p1", generation_id="g0", state="retired",
                        lease_count=1, can_quiesce=False,
                    ),
                ),
            ),
        )
        payload = DashboardService(workspace=Path("."), plugin_manager=manager).plugins()
        self.assertEqual(payload["generations"][0]["lease_count"], 2)
        # 仍有在途租约的退休代际不可销毁——这正是热重载不抽走能力的运行时证据
        self.assertFalse(payload["retired"][0]["can_quiesce"])
        self.assertEqual(payload["errors"], {"bad": "boom"})


class WebRoutingTests(unittest.TestCase):
    """Web 渠道未注入 dashboard 时也要能自建一个,页面不至于打不开。"""

    def test_channel_builds_fallback_dashboard(self) -> None:
        from kirakira_agent.channels.web import WebChannel

        channel = WebChannel()
        board = channel._dashboard()
        self.assertIsInstance(board, DashboardService)
        self.assertEqual(board.memories({})["total"], 0)

    def test_injected_dashboard_is_used(self) -> None:
        from kirakira_agent.channels.web import WebChannel

        with tempfile.TemporaryDirectory() as tmp:
            injected = DashboardService(workspace=Path(tmp))
            channel = WebChannel(dashboard=injected)
            self.assertIs(channel._dashboard(), injected)


class PageTests(unittest.TestCase):
    def test_pages_are_self_contained(self) -> None:
        from kirakira_agent.channels.web_ui import CHAT_HTML, DASHBOARD_HTML

        for page in (CHAT_HTML, DASHBOARD_HTML):
            self.assertTrue(page.startswith("<!doctype html>"))
            # 零依赖是刻意的:不引外部 CDN,否则离线环境页面会半坏
            self.assertNotIn("https://cdn", page)
            self.assertNotIn("<script src=", page)
            self.assertIn("prefers-color-scheme", page)  # 深浅色都要有

    def test_dashboard_declares_all_panels(self) -> None:
        from kirakira_agent.channels.web_ui import DASHBOARD_HTML

        for tab in ("overview", "memory", "sessions", "plugins", "proactive"):
            self.assertIn('data-tab="%s"' % tab, DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
