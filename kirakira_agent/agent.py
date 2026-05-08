from pathlib import Path
from typing import List, Optional

from kirakira_agent.context import compact_messages, estimate_tokens, microcompact
from kirakira_agent.models.base import ModelClient
from kirakira_agent.schema import JsonDict, ModelResponse, assistant_message_from_response, tool_result_message
from kirakira_agent.tools.registry import ToolRegistry


DEFAULT_SYSTEM = (
    "You are a coding agent. You can use tools to solve tasks. "
    "Act when tool use helps, and give a concise final answer when done."
)


class Agent:
    def __init__(
        self,
        model_client: ModelClient,
        tool_registry: ToolRegistry,
        model: str,
        workdir: Path,
        system: str = DEFAULT_SYSTEM,
        max_tokens: int = 8000,
        token_threshold: int = 100000,
    ) -> None:
        self.model_client = model_client
        self.tool_registry = tool_registry
        self.model = model
        self.workdir = workdir
        self.system = system
        self.max_tokens = max_tokens
        self.token_threshold = token_threshold
        self.transcript_dir = workdir / ".transcripts"

    def run(self, messages: List[JsonDict], max_rounds: int = 50) -> ModelResponse:
        last_response: Optional[ModelResponse] = None
        for _ in range(max_rounds):
            microcompact(messages)
            if estimate_tokens(messages) > self.token_threshold:
                messages[:] = self.compact(messages)

            response = self.model_client.complete(
                messages=messages,
                tools=self.tool_registry.specs(),
                system=self.system,
                model=self.model,
                max_tokens=self.max_tokens,
            )
            last_response = response
            messages.append(assistant_message_from_response(response))

            if not response.has_tool_calls:
                return response

            requested_compact = False
            for call in response.tool_calls:
                result = self.tool_registry.execute(call)
                if call.name == "compact":
                    requested_compact = True
                messages.append(tool_result_message(result))

            if requested_compact:
                messages[:] = self.compact(messages)
                return ModelResponse(text="Context compacted.", stop_reason="end_turn")

        return last_response or ModelResponse(text="", stop_reason="max_rounds")

    def compact(self, messages: List[JsonDict]) -> List[JsonDict]:
        def summarize(text: str) -> str:
            prompt = (
                "Summarize this agent conversation for continuity. "
                "Preserve goals, decisions, changed files, open tasks, and important tool results.\n\n"
                + text
            )
            response = self.model_client.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                system="You compress agent transcripts into concise continuity summaries.",
                model=self.model,
                max_tokens=min(self.max_tokens, 2000),
            )
            return response.text or "Conversation compressed."

        return compact_messages(messages, self.transcript_dir, summarizer=summarize)
