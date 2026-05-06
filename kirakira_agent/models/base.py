from typing import List, Protocol

from kirakira_agent.schema import JsonDict, ModelResponse, ToolSpec


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
