"""Lost sidecars rebuild from actual online vector inputs without a provider."""

import hashlib
import json

import pytest

from eval.longmemeval.offline_ab import DeterministicEmbedder
from plugins.akasha2.engine import Akasha2MemoryEngine
from plugins.akasha2.infrastructure.persistence import logical_state_sha256
from plugins.default_memory.config_models import (
    Config,
    MemoryConfig,
    MemoryEmbeddingConfig,
)
from plugins.ledger.embedding_store import MessageEmbeddingStore
from plugins.ledger.manager import SessionManager
from scripts.replay_akasha2 import rebuild
from tests.test_akasha2_runtime import add


class Provider(DeterministicEmbedder):
    def __init__(self, forbidden=False):
        super().__init__()
        self.forbidden = forbidden

    async def embed_batch(self, texts):
        if self.forbidden:
            self.calls += 1
            raise AssertionError("recovery contacted an embedding provider")
        return await super().embed_batch(texts)


def open_engine(path, provider, dimension=64):
    return Akasha2MemoryEngine(
        config=Config(
            model="unused",
            memory=MemoryConfig(
                embedding=MemoryEmbeddingConfig(
                    model=provider.model_id,
                    base_url="offline://fixed",
                    output_dimensionality=dimension,
                )
            ),
        ),
        workspace=path,
        http_resources=None,
        embedder=provider,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", [64, None])
async def test_failed_learning_reuses_vectors_and_lost_graph_rebuilds_offline(
    tmp_path, dimension
):
    sessions = SessionManager(tmp_path)
    provider = Provider()
    memory = open_engine(tmp_path, provider, dimension)
    for index in range(2):
        add(sessions, f"fixture:{index}", f"original episode {index}")
    await memory.catch_up()
    memory._publish()
    add(sessions, "fixture:tail", "a cached tail survives a failed commit")

    def fail(*args):
        raise RuntimeError("planned learning interruption")

    memory._cycle.commit = fail
    with pytest.raises(RuntimeError, match="planned learning interruption"):
        await memory.catch_up()
    assert provider.calls == 3
    await memory.aclose()
    forbidden = Provider(forbidden=True)
    recovered = open_engine(tmp_path, forbidden, dimension)
    assert await recovered.catch_up() == 1
    assert forbidden.calls == 0
    await recovered.aclose()
    sessions.close()
    graph = tmp_path / "memory/akasha2.db"
    expected = logical_state_sha256(graph)
    graph.unlink()  # Only this isolated test's derived graph is removed.
    source = tmp_path / "sessions.db"
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate = tmp_path / "candidate.db"
    result = await rebuild(source, candidate, provider.model_id, "offline://fixed")
    assert result["cache_identity"] == "full_space"
    assert result["learned_turns"] == 3
    assert logical_state_sha256(candidate) == expected
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest


@pytest.mark.asyncio
async def test_online_cache_does_not_reuse_another_endpoint_space(tmp_path):
    sessions = SessionManager(tmp_path)
    provider = Provider()
    memory = open_engine(tmp_path, provider)
    add(sessions)
    messages = sessions.get_or_create("test:1").messages
    other_space = json.loads(memory._space)
    other_space["endpoint"] = "offline://another-provider"
    other_space = json.dumps(other_space, sort_keys=True)
    store = MessageEmbeddingStore(tmp_path / "sessions.db")
    try:
        for message in messages:
            store.upsert(
                message_id=message["id"],
                content=message["content"],
                model=other_space,
                embedding=[1.0] + [0.0] * 63,
            )
        await memory.catch_up()
        assert provider.calls == 1
        for message in messages:
            assert (
                store.get(
                    message_id=message["id"],
                    content=message["content"],
                    model=memory._space,
                )
                is not None
            )
            assert (
                store.get(
                    message_id=message["id"],
                    content=message["content"],
                    model=other_space,
                )
                == [1.0] + [0.0] * 63
            )
    finally:
        store.close()
        await memory.aclose()
        sessions.close()
