"""Kirakira Agent learning harness module."""

from typing import Any, List, Protocol

from core.schema import JsonDict, ModelResponse, ToolSpec
from plugins.models.usage import ModelUsage, UsageCoverage


class ModelRequestError(RuntimeError):
    """Base class for provider errors with runtime handling semantics."""


class ContextLengthError(ModelRequestError):
    pass


class ContentSafetyError(ModelRequestError):
    pass


class RetryableModelError(ModelRequestError):
    """A provider transport failure, never an arbitrary internal exception."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


class ModelRetriesExhausted(RetryableModelError):
    def __init__(self, attempts: int, last_error: Exception):
        super().__init__(
            f"Upstream failed after {attempts} attempts; completed tool results are retained: {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


class ModelOutputTruncated(ModelRequestError):
    """An incomplete output cannot authorize any requested tools."""

    def __init__(self, message, *, response=None):
        super().__init__(message)
        self.response = response


class ModelClient(Protocol):
    def complete(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        model: str,
        max_tokens: int,
        tool_choice: Any = "auto",
    ) -> ModelResponse:
        """tool_choice 照 Reference provider.chat:"auto" / "required" /
        {"type":"function","function":{"name":...}}。调用方只在需要强制时才传,
        因此仅实现旧签名的测试替身在不使用该能力时依然兼容。"""
        ...


__all__ = [
    "ContentSafetyError",
    "ContextLengthError",
    "ModelClient",
    "ModelRequestError",
    "ModelRetriesExhausted",
    "ModelOutputTruncated",
    "ModelUsage",
    "RetryableModelError",
    "UsageCoverage",
]
