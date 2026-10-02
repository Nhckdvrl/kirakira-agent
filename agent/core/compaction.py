"""Adapt the shared model invoker to query-local compaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.core.reasoner import DefaultReasoner

from agent.model_runtime.context_policy import estimate_context_tokens


class _CompactionProviderAdapter:
    """Expose Kirakira's model client through Reference's compactor contract."""

    runtime_id = "main"

    def __init__(self, reasoner: "DefaultReasoner") -> None:
        self._reasoner = reasoner
        self.context_window = max(0, int(reasoner.config.context_window))
        self.max_output_tokens = max(0, int(reasoner.config.max_tokens))
        self.model = reasoner.config.model

    def estimate_context_tokens(self, messages: list[dict], tools: list[dict]) -> int:
        return estimate_context_tokens(messages, tools)

    def estimate_appended_message_tokens(self, messages: list[dict]) -> int:
        return estimate_context_tokens(messages, []) if messages else 0
