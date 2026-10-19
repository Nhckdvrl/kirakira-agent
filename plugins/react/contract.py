"""Configuration and results exchanged between runtime owners."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from core.schema import JsonDict
from plugins.models.retry import ModelRetryPolicy


@dataclass
class ReactConfig:
    model: str
    context_window: int = 0
    effective_context_percent: float = 0.9
    max_iterations: int = 10
    max_tokens: int = 8192
    history_window: int = 40
    model_timeout_seconds: float = 120.0
    repeated_tool_call_limit: int = 3
    max_parallel_tool_calls: int = 4
    stream: bool = True
    retry_policy: ModelRetryPolicy = field(default_factory=ModelRetryPolicy)


@dataclass
class ReactResult:
    reply: str
    tools_used: List[str] = field(default_factory=list)
    tool_chain: List[JsonDict] = field(default_factory=list)
    thinking: str = ""
    context_trace: JsonDict = field(default_factory=dict)
    # 本轮是否有工具声明需要用户确认;None 表示没有。
    mobile_attention: Optional[str] = None
    execution_id: str = ""
    stop_reason: str = "completed"


from agent.composition import ServiceKey

REACT = ServiceKey("react.execute.v1")
REACT_CONFIG = ServiceKey("react.config.v1")
