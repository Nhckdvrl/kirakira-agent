"""Kirakira Agent learning harness module."""

from .base import ModelClient
from .openai_compatible import OpenAICompatibleClient

__all__ = ["ModelClient", "OpenAICompatibleClient"]
