"""Execution policy shared by interactive and background reasoning.

Visibility is not authorization: a provider may emit an unadvertised call.
The reasoner enforces this policy again immediately before execution.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ReasoningPolicy:
    allowed_tools: frozenset[str] | None = None
    terminal_tools: frozenset[str] = frozenset()
    tool_choice: object | None = None
    final_tool_choice: object | None = None
    summarize_on_exhaustion: bool = True
    # Decision tools must be the sole call in a batch. Reject the whole batch
    # before executing anything, rather than partially applying a decision.
    exclusive_terminal: bool = False
    # A program may narrow its grant after a successful action. The executor
    # re-reads it between calls, including calls in the same provider batch.
    current_tools: Callable[[], frozenset[str]] | None = None
    check_current: Callable[[], None] | None = None

    def allows(self, name: str) -> bool:
        return (self.allowed_tools is None or name in self.allowed_tools) and (
            self.current_tools is None or name in self.current_tools()
        )
