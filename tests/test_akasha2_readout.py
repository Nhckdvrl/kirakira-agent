"""A graph miss cannot erase exact evidence; both lanes retain source integrity."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.memory.engine import MemoryQuery, MemoryQueryFilters
from plugins.akasha2.domain.readout import RecallItem
from plugins.akasha2.engine import Akasha2MemoryEngine
from plugins.akasha2.infrastructure.persistence import logical_state_sha256
from plugins.default_memory.config_models import (
    Config,
    MemoryConfig,
    MemoryEmbeddingConfig,
)
from plugins.ledger.manager import SessionManager


class Embeddings:
    model_id = "controlled-readout-fixture"

    async def embed(self, text):
        return [1.0, 0.0] if "ALPHA" in text else [0.0, 1.0]

    async def embed_batch(self, texts):
        return [await self.embed(text) for text in texts]

    async def aclose(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_hit", [False, True])
async def test_exact_lane_survives_graph_miss_and_deduplicates_completion(
    tmp_path, graph_hit
):
    sessions = SessionManager(tmp_path)
    engine = Akasha2MemoryEngine(
        config=Config(
            model="fixture",
            memory=MemoryConfig(
                embedding=MemoryEmbeddingConfig(
                    model=Embeddings.model_id,
                    base_url="offline://fixture",
                    output_dimensionality=2,
                )
            ),
        ),
        workspace=tmp_path,
        http_resources=SimpleNamespace(),
        embedder=Embeddings(),
    )
    try:
        session = sessions.get_or_create("fixture:source")
        for index in range(8):
            stamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
            text = "release code ALPHA" if index == 6 else f"unrelated item {index}"
            session.add_message("user", text, timestamp=stamp.isoformat())
            session.add_message(
                "assistant", "recorded " + text, timestamp=stamp.isoformat()
            )
            sessions.save(session)
        await engine.catch_up()
        engine._publish()
        before = logical_state_sha256(tmp_path / "memory/akasha2.db")
        original = engine._cycle.retrieve

        def completion_fixture(*args, **kwargs):
            ticket = original(*args, **kwargs)
            items = (
                (
                    RecallItem(6, 1.0, ("sharp_completion",), ()),
                    RecallItem(7, 0.5, ("sharp_completion",), ()),
                )
                if graph_hit
                else ()
            )
            return replace(ticket, completion=replace(ticket.completion, items=items))

        engine._cycle.retrieve = completion_fixture
        query = MemoryQuery(
            "What is the ALPHA release code?",
            limit=6,
            effect="read_only",
            timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        result = await engine.query(query)
        assert len(result.records) == (6 if graph_hit else 5)
        exact = [r for r in result.records if r.signals["lane"] == "dense"]
        assert len(exact) == 5 and "ALPHA" in exact[0].summary
        assert len({record.id for record in result.records}) == len(result.records)
        if graph_hit:
            assert result.records[-1].signals["lane"] == "completion"
            assert "unrelated item 7" in result.records[-1].summary
        filtered = await engine.query(
            replace(
                query,
                limit=2,
                filters=MemoryQueryFilters(
                    relevance_floor="strong",
                    time_start=datetime(2026, 1, 5, tzinfo=timezone.utc),
                ),
            )
        )
        assert len(filtered.records) == 2 and all(
            r.signals["lane"] == "dense" for r in filtered.records
        )
        assert all("unrelated item 0" not in r.summary for r in filtered.records)
        assert logical_state_sha256(tmp_path / "memory/akasha2.db") == before
    finally:
        await engine.aclose()
        sessions.close()
