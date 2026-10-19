"""Run a bounded background program through the production reasoner.

The caller owns input persistence, tool grants and delivery. This function does
not append synthetic user messages to a conversation or trigger memory learning.
"""

from bus.event_bus import EventBus
from plugins.execution.service import ExecutionStore
from plugins.react.contract import ReactConfig, ReactResult
from plugins.react.policy import ReasoningPolicy
from plugins.react.reasoner import ReactProgram
from plugins.tools.registry import ToolRegistry


async def run_scoped(
    *,
    model_client,
    registry: ToolRegistry,
    model: str,
    system: str,
    prompt: str,
    session_key: str,
    max_steps: int,
    max_tokens: int,
    policy: ReasoningPolicy,
    channel: str = "internal",
    chat_id: str = "",
    execution_store: ExecutionStore | None = None,
) -> ReactResult:
    if max_steps < 1:
        raise ValueError("scoped reasoning requires a positive step budget")
    reasoner = ReactProgram(
        model_client=model_client,
        tools=registry,
        config=ReactConfig(model=model, max_tokens=max_tokens, stream=False),
        context=None,
        event_bus=EventBus(),
        execution_store=execution_store,
    )
    token = registry.set_context(
        channel=channel, chat_id=chat_id, session_key=session_key
    )
    try:
        return await reasoner.run(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            request_text=prompt,
            max_iterations_override=max_steps,
            policy=policy,
        )
    finally:
        registry.reset_context(token)
