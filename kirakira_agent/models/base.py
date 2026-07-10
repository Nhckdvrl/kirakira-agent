"""Kirakira Agent learning harness module."""

from typing import List, Protocol

from kirakira_agent.schema import JsonDict, ModelResponse, ToolSpec


class ModelRequestError(RuntimeError):
    """Base class for provider errors with runtime handling semantics."""


class ContextLengthError(ModelRequestError):
    pass


class ContentSafetyError(ModelRequestError):
    pass


class ModelClient(Protocol):
    def complete(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        model: str,
        max_tokens: int,
    ) -> ModelResponse:
        ...
