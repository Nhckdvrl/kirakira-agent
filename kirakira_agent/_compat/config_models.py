"""Configuration subset consumed by the pinned Reference memory engine."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemoryEmbeddingConfig:
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    output_dimensionality: int | None = None


@dataclass
class MemoryConfig:
    enabled: bool = True
    engine: str = "default"
    embedding: MemoryEmbeddingConfig = field(default_factory=MemoryEmbeddingConfig)


@dataclass
class Config:
    model: str
    api_key: str = ""
    base_url: str | None = None
    light_model: str = ""
    light_api_key: str = ""
    light_base_url: str = ""
    memory: MemoryConfig = field(default_factory=MemoryConfig)
