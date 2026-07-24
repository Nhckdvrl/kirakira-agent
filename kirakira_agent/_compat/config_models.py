"""Configuration subset consumed by the pinned Reference memory engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


def build_config(app_config: dict[str, Any]) -> Config:
    """从 kirakira 的 config.toml dict 构建引擎所需的 Config。

    kirakira 只有单一主模型,没有独立 light 模型,故 light_* 回退到主模型。
    差异隔离在此适配器内,引擎照抄 Reference 不动。
    """

    from kirakira_agent.config import config_value

    model = str(config_value(app_config, "llm", "main", "model", default=""))
    base_url = str(config_value(app_config, "llm", "main", "base_url", default=""))
    api_key = str(config_value(app_config, "llm", "main", "api_key", default=""))

    light_model = str(
        config_value(app_config, "llm", "light", "model", default="") or model
    )
    light_base_url = str(
        config_value(app_config, "llm", "light", "base_url", default="") or base_url
    )
    light_api_key = str(
        config_value(app_config, "llm", "light", "api_key", default="") or api_key
    )

    embed_dim_raw = config_value(
        app_config, "memory", "embedding", "output_dimensionality", default=None
    )
    embedding = MemoryEmbeddingConfig(
        model=str(config_value(app_config, "memory", "embedding", "model", default="")),
        api_key=str(config_value(app_config, "memory", "embedding", "api_key", default="")),
        base_url=str(config_value(app_config, "memory", "embedding", "base_url", default="")),
        output_dimensionality=int(embed_dim_raw)
        if embed_dim_raw not in (None, "")
        else None,
    )
    memory = MemoryConfig(
        enabled=bool(config_value(app_config, "memory", "enabled", default=True)),
        engine=str(config_value(app_config, "memory", "engine", default="default")),
        embedding=embedding,
    )
    return Config(
        model=model,
        api_key=api_key,
        base_url=base_url,
        light_model=light_model,
        light_api_key=light_api_key,
        light_base_url=light_base_url,
        memory=memory,
    )
