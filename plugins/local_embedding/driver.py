"""Hash-verified model assets; network downloads belong to explicit setup only."""

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import numpy as np

from core.common.file_io import run_file_io
from plugins.models.contract import (
    CapabilitySources,
    DiscoveredModel,
    DriverConnection,
    EmbeddingResult,
    ModelCapabilities,
    ModelDriverDefinition,
)

DRIVER_ID = "local-embedding"
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_assets(descriptor):
    uri = urlsplit(descriptor.endpoint)
    if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
        raise ValueError("local embeddings require an absolute file: asset manifest")
    path = Path(unquote(uri.path))
    if not path.is_absolute() or sha256(path) != descriptor.config.get("asset_sha256"):
        raise ValueError("local embedding asset manifest changed")
    value = json.loads(path.read_text())
    directory = Path(value["directory"]).resolve()
    if not directory.is_relative_to(path.parent.resolve()) or not value["files"]:
        raise ValueError("embedding model assets must remain inside their cache root")
    for name, expected in value["files"].items():
        file = directory / name
        if (
            not file.resolve().is_relative_to(path.parent.resolve())
            or sha256(file) != expected
        ):
            raise ValueError("local embedding weights or tokenizer changed")
    return value


class Encoder:
    def __init__(self, assets, threads):
        self.assets, self.threads = assets, threads
        self.model = None
        self.lock = asyncio.Lock()

    def _embed(self, texts):
        if self.model is None:
            from fastembed import TextEmbedding

            self.model = TextEmbedding(
                self.assets["model"],
                specific_model_path=self.assets["directory"],
                local_files_only=True,
                threads=self.threads,
                providers=["CPUExecutionProvider"],
            )
        if not texts:
            return EmbeddingResult(())
        vectors = np.asarray(
            list(self.model.embed(texts, batch_size=32)), dtype=np.float32
        )
        if vectors.shape != (len(texts), self.assets["dimensions"]) or not np.all(
            np.isfinite(vectors)
        ):
            raise ValueError("local encoder returned invalid vectors")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError("local encoder returned a zero vector")
        return EmbeddingResult(
            tuple(tuple(float(v) for v in row) for row in vectors / norms)
        )

    async def embed(self, texts):
        # Cancellation drains the CPU work before a generation can release its
        # encoder. Use the host's bounded, cancellation-draining worker helper.
        async with self.lock:
            return await run_file_io(lambda: self._embed(list(texts)))


async def open_connection(descriptor, _credential):
    if descriptor.driver_id != DRIVER_ID:
        raise ValueError("local embedding connection belongs to another driver")
    assets = await run_file_io(lambda: read_assets(descriptor))
    threads = descriptor.config.get("threads", 2)
    if type(threads) is not int or not 1 <= threads <= 16:
        raise ValueError("local embedding threads must be between 1 and 16")
    encoder = Encoder(assets, threads)

    def embedding(model, _config):
        if (
            model.model != assets["model"]
            or model.dimensions != assets["dimensions"]
            or model.normalization != "l2"
        ):
            raise ValueError(
                "embedding binding does not match the pinned local vector space"
            )
        return encoder

    def chat(*_args):
        raise ValueError("local-embedding grants no chat model")

    return DriverConnection(chat, embedding)


async def discover(descriptor, _credential):
    assets = await run_file_io(lambda: read_assets(descriptor))
    return (
        DiscoveredModel(
            kind="embedding",
            model=assets["model"],
            capabilities=ModelCapabilities(
                embedding_dimensions=assets["dimensions"], embedding_normalization="l2"
            ),
            capability_sources=CapabilitySources(
                embedding_dimensions="driver", embedding_normalization="driver"
            ),
        ),
    )


def definition():
    return ModelDriverDefinition(DRIVER_ID, "1", open_connection, discover=discover)
