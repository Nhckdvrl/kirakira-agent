import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from kirakira_agent.schema import JsonDict, ModelResponse, ToolCall, ToolSpec


class OpenAICompatibleClient:
    """Client for OpenAI-compatible /v1/chat/completions endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
    ) -> None:
        self.base_url = (base_url or os.getenv("OPENAI_COMPATIBLE_BASE_URL") or "").rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
        self.timeout = timeout
        if not self.base_url:
            raise ValueError("OPENAI_COMPATIBLE_BASE_URL is required")

    def complete(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        model: str,
        max_tokens: int,
    ) -> ModelResponse:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [self._to_openai_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._chat_completions_url(),
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("Model request failed: HTTP %s %s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("Model request failed: %s" % exc.reason) from exc

        return self.parse_response(json.loads(body))

    def parse_response(self, payload: JsonDict) -> ModelResponse:
        choices = payload.get("choices") or []
        if not choices:
            return ModelResponse(text="", stop_reason="empty", raw=payload)

        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        tool_calls = []
        for raw_call in message.get("tool_calls") or []:
            fn = raw_call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {"_raw": raw_args}
            tool_calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments=args,
                )
            )

        finish_reason = choice.get("finish_reason") or ""
        stop_reason = "tool_use" if tool_calls or finish_reason == "tool_calls" else "end_turn"
        return ModelResponse(text=text, tool_calls=tool_calls, stop_reason=stop_reason, raw=payload)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        return headers

    def _chat_completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def _to_openai_tool(self, tool: ToolSpec) -> JsonDict:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    def _to_openai_messages(self, messages: List[JsonDict], system: str) -> List[JsonDict]:
        converted: List[JsonDict] = []
        if system:
            converted.append({"role": "system", "content": system})
        for msg in messages:
            role = msg.get("role")
            if role == "tool":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id"),
                        "content": msg.get("content", ""),
                    }
                )
                continue
            if role == "assistant" and msg.get("tool_calls"):
                converted.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": call.get("id"),
                                "type": "function",
                                "function": {
                                    "name": (call.get("function") or {}).get("name"),
                                    "arguments": json.dumps((call.get("function") or {}).get("arguments", {})),
                                },
                            }
                            for call in msg.get("tool_calls", [])
                        ],
                    }
                )
                continue
            converted.append({"role": role, "content": msg.get("content", "")})
        return converted
