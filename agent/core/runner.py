"""Synchronous convenience API over the canonical asynchronous reasoning engine."""

import asyncio
from pathlib import Path
from uuid import uuid4

from agent.core.contracts import RuntimeConfig
from agent.core.execution import ExecutionStore
from agent.core.model_io import ModelInvoker
from agent.core.reasoner import DefaultReasoner
from agent.model_runtime.context import compact_messages, estimate_tokens
from agent.turns.policy import ReasoningPolicy
from bus.event_bus import EventBus
from core.schema import ModelResponse

DEFAULT_SYSTEM = (
    "You are a coding agent. You can use tools to solve tasks. "
    "Act when tool use helps, and give a concise final answer when done."
)


class Agent:
    """Owns no model/tool loop. Async clients should call arun directly."""

    def __init__(
        self,
        model_client,
        tool_registry,
        model: str,
        workdir: Path,
        system: str = DEFAULT_SYSTEM,
        max_tokens: int = 8000,
        token_threshold: int = 100000,
    ):
        self.model_client, self.tool_registry = model_client, tool_registry
        self.model, self.workdir, self.system = model, workdir, system
        self.max_tokens, self.token_threshold = max_tokens, token_threshold
        self.transcript_dir = workdir / ".transcripts"
        self._session_key = "local:" + uuid4().hex

    def run(self, messages, *args, **kwargs) -> ModelResponse:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun(messages, *args, **kwargs))
        raise RuntimeError(
            "Agent.run cannot nest an event loop; await Agent.arun instead"
        )

    async def arun(
        self,
        messages,
        max_rounds=50,
        tool_choice=None,
        final_tool_choice=None,
        stop_tools=frozenset(),
    ) -> ModelResponse:
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if estimate_tokens(messages) > self.token_threshold:
            messages[:] = await self._compact(messages)
        payload = [{"role": "system", "content": self.system}, *messages]
        store = ExecutionStore(self.workdir / ".kirakira" / "executions.db")
        reasoner = DefaultReasoner(
            model_client=self.model_client,
            tools=self.tool_registry,
            config=RuntimeConfig(
                model=self.model, max_tokens=self.max_tokens, stream=False
            ),
            context=None,
            event_bus=EventBus(),
            execution_store=store,
        )
        token = self.tool_registry.set_context(
            session_key=self._session_key, channel="local", chat_id="local"
        )
        try:
            result = await reasoner.run(
                payload,
                session_key=self._session_key,
                channel="local",
                chat_id="local",
                request_text=str(messages[-1].get("content", "")) if messages else "",
                max_iterations_override=max_rounds,
                policy=ReasoningPolicy(
                    terminal_tools=frozenset(stop_tools),
                    tool_choice=tool_choice,
                    final_tool_choice=final_tool_choice,
                    summarize_on_exhaustion=False,
                ),
            )
            return ModelResponse(
                text=result.reply,
                reasoning_content=result.thinking,
                stop_reason=result.stop_reason,
                raw={"execution_id": result.execution_id},
            )
        finally:
            messages[:] = payload[1:]
            try:
                await self.tool_registry.cleanup_owner(self._session_key)
            finally:
                self.tool_registry.reset_context(token)
                store.close()

    def compact(self, messages):
        return asyncio.run(self._compact(messages))

    async def _compact(self, messages):
        invoker = ModelInvoker(
            self.model_client,
            RuntimeConfig(
                model=self.model, max_tokens=min(self.max_tokens, 2000), stream=False
            ),
            EventBus(),
        )
        response = await invoker.complete(
            [
                {
                    "role": "system",
                    "content": "Summarize this conversation, preserving goals, decisions, files and unfinished tasks.",
                },
                *messages,
            ],
            [],
            session_key=self._session_key,
            channel="local",
            chat_id="local",
            iteration=-1,
        )
        return compact_messages(
            messages, self.transcript_dir, summarizer=lambda _: response.text
        )
