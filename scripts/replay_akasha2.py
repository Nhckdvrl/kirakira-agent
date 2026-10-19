"""Build a NEW Akasha2 checkpoint from fixed vectors in a read-only source backup.

No model calls, network requests, source changes, overwrite, or auto-activation.
The endpoint identifies the existing vector space; it is never contacted.
"""

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import numpy as np
from agent.config_models import Config, MemoryConfig, MemoryEmbeddingConfig
from plugins.akasha2.engine import Akasha2MemoryEngine, _vector
from session.turn_source import load_committed_turn_messages
from bus.events_lifecycle import TurnCommitted
from types import SimpleNamespace


async def rebuild(source, output, model, embedding_endpoint):
    if output.exists():
        raise FileExistsError("replay requires a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".akasha2-replay-", dir=output.parent
    ) as temporary:
        work = Path(temporary)
        snapshot = work / "sessions.db"
        with closing(
            sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        ) as live:
            with closing(sqlite3.connect(snapshot)) as copy:
                live.backup(copy)
        vectors, dimensions = {}, set()
        with closing(sqlite3.connect(snapshot)) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute(
                "SELECT p.message_id,p.session_key FROM message_publications p ORDER BY p.position"
            ).fetchall():
                if row["session_key"].startswith("scheduler:"):
                    continue
                messages = load_committed_turn_messages(
                    snapshot,
                    TurnCommitted(
                        session_key=row["session_key"],
                        channel="replay",
                        chat_id="",
                        assistant_message_id=row["message_id"],
                    ),
                )
                for message in messages:
                    cached = db.execute(
                        "SELECT embedding,dim FROM message_embeddings "
                        "WHERE message_id=? AND model=? AND content_hash=?",
                        (
                            message.id,
                            model,
                            hashlib.sha256(message.content.encode()).hexdigest(),
                        ),
                    ).fetchone()
                    if cached is None:
                        raise ValueError(
                            "missing fixed embedding; no network fallback is allowed"
                        )
                    vector = np.frombuffer(cached["embedding"], dtype="<f4").copy()
                    if len(vector) != cached["dim"]:
                        raise ValueError("corrupt fixed embedding")
                    _vector(vector)
                    dimensions.add(len(vector))
                    previous = vectors.get(message.content)
                    if previous is not None and not np.array_equal(previous, vector):
                        raise ValueError(
                            "same text has different fixed vectors within one model space"
                        )
                    vectors[message.content] = vector
        if len(dimensions) != 1:
            raise ValueError("replay requires a nonempty, uniform embedding space")

        class FixedVectors:
            model_id = model

            async def embed_batch(self, texts):
                return [vectors[text].tolist() for text in texts]

        config = Config(
            model="offline",
            memory=MemoryConfig(
                embedding=MemoryEmbeddingConfig(
                    model=model,
                    base_url=embedding_endpoint,
                    output_dimensionality=dimensions.pop(),
                )
            ),
        )
        engine = Akasha2MemoryEngine(
            config=config,
            workspace=work,
            http_resources=SimpleNamespace(),
            embedder=FixedVectors(),
            rebuild_from_start=True,
        )
        try:
            while await engine.catch_up():
                pass
            learned = engine._cycle.state_version
            cursor = engine._checkpoint.cursor
        finally:
            await engine.aclose()
        candidate = work / "memory/akasha2.db"
        # Publish without replacement, even if another process creates the output.
        with candidate.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(candidate, output)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return {
        "learned_turns": learned,
        "source_cursor": cursor,
        "model": model,
        "output": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument(
        "--embedding-endpoint",
        required=True,
        help="Exact existing vector-space endpoint; never contacted",
    )
    a = p.parse_args()
    print(
        json.dumps(
            asyncio.run(rebuild(a.source, a.output, a.model, a.embedding_endpoint)),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
