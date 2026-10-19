"""Phase 2 记忆 DI 缝 + 引擎工厂门控。

- 配了 [memory.embedding] → DefaultMemoryEngine(有检索能力);
- 没配 → DisabledMemoryEngine(能力集为空,pipeline 回退旧词法路径)。
runtime 的 _engine_can_retrieve 据此决定检索走引擎还是旧路径。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("isolated_cwd")

import tempfile
import unittest
from pathlib import Path

from core.memory.plugin import DisabledMemoryEngine
from infra.providers.model_client_adapter import ModelClientProvider
from plugins.default_memory.config_models import build_config
from plugins.default_memory.contract import MemoryServices
from plugins.default_memory.engine import DefaultMemoryEngine
from plugins.default_memory.factory import build_memory_engine, memory_engine_enabled
from plugins.reply_program.service import _engine_can_retrieve


class _StubClient:
    def complete(self, messages, tools, system, model, max_tokens):
        raise AssertionError("not called in these tests")


def _provider() -> ModelClientProvider:
    return ModelClientProvider(_StubClient())


class MemoryFactoryGateTests(unittest.TestCase):
    def test_no_embedding_yields_disabled_engine(self) -> None:
        engine = build_memory_engine(
            app_config={"memory": {"enabled": True, "embedding": {"base_url": ""}}},
            workspace=Path("."),
            provider=_provider(),
        )
        self.assertIsInstance(engine, DisabledMemoryEngine)
        self.assertFalse(_engine_can_retrieve(engine))

    def test_configured_embedding_yields_default_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = build_memory_engine(
                app_config={
                    "llm": {
                        "main": {
                            "model": "m",
                            "base_url": "http://x/v1",
                            "api_key": "k",
                        }
                    },
                    "memory": {
                        "enabled": True,
                        "embedding": {
                            "model": "text-embedding-v3",
                            "base_url": "http://x/v1",
                            "api_key": "k",
                        },
                    },
                },
                workspace=Path(tmp),
                provider=_provider(),
            )
            try:
                self.assertIsInstance(engine, DefaultMemoryEngine)
                self.assertTrue(_engine_can_retrieve(engine))
            finally:
                engine._v2_store.close()

    def test_memory_engine_enabled_requires_embedding_base_url(self) -> None:
        cfg_on = build_config(
            {"memory": {"enabled": True, "embedding": {"base_url": "http://x/v1"}}}
        )
        cfg_off = build_config(
            {"memory": {"enabled": True, "embedding": {"base_url": ""}}}
        )
        self.assertTrue(memory_engine_enabled(cfg_on))
        self.assertFalse(memory_engine_enabled(cfg_off))


import asyncio
import unittest


class _Closeable:
    def __init__(self, name: str, sink: list[str], *, boom: bool = False) -> None:
        self.name = name
        self._sink = sink
        self._boom = boom

    def close(self) -> None:
        if self._boom:
            raise RuntimeError("close failed: %s" % self.name)
        self._sink.append(self.name)


class _AsyncCloseable:
    def __init__(self, name: str, sink: list[str]) -> None:
        self.name = name
        self._sink = sink

    async def aclose(self) -> None:
        self._sink.append(self.name)


class _Engine:
    def __init__(self, closeables: list[object]) -> None:
        self.closeables = closeables


class MemoryServicesShutdownTests(unittest.TestCase):
    def test_closeables_released_in_reverse_order(self) -> None:
        async def scenario() -> None:
            closed: list[str] = []
            engine = _Engine(
                [_Closeable("store", closed), _AsyncCloseable("embedder", closed)]
            )
            await MemoryServices(engine=engine).aclose()
            # 逆序释放:后建的先关
            self.assertEqual(closed, ["embedder", "store"])

        asyncio.run(scenario())

    def test_one_failure_does_not_block_the_rest_and_is_raised(self) -> None:
        async def scenario() -> None:
            closed: list[str] = []
            engine = _Engine(
                [
                    _Closeable("store", closed),
                    _Closeable("bad", closed, boom=True),
                    _AsyncCloseable("embedder", closed),
                ]
            )
            with self.assertRaises(RuntimeError):
                await MemoryServices(engine=engine).aclose()
            # 失败的那个跳过,其余仍被关闭
            self.assertEqual(closed, ["embedder", "store"])

        asyncio.run(scenario())
