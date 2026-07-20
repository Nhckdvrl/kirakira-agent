"""Kirakira Agent learning harness module."""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from kirakira_agent.schema import JsonDict, ModelResponse, ToolCall, ToolSpec
from kirakira_agent.context_policy import (
    build_runtime_context_budget,
    estimate_context_tokens,
)
from kirakira_agent.models.base import ContentSafetyError, ContextLengthError


class OpenAICompatibleClient:
    """Client for OpenAI-compatible /v1/chat/completions endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
        thinking_enabled: Optional[bool] = None,
        context_window: int = 0,
        effective_context_percent: float = 0.9,
    ) -> None:
        self.base_url = (base_url or os.getenv("OPENAI_COMPATIBLE_BASE_URL") or "").rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
        self.timeout = timeout
        self.thinking_enabled = thinking_enabled
        self.context_window = int(context_window)
        self.effective_context_percent = float(effective_context_percent)
        if not self.base_url:
            raise ValueError("OPENAI_COMPATIBLE_BASE_URL is required")
        if self.context_window < 0:
            raise ValueError("context_window must not be negative")
        if not 0 < self.effective_context_percent <= 1:
            raise ValueError("effective_context_percent must be within (0, 1]")

    def complete(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        model: str,
        max_tokens: int,
    ) -> ModelResponse:
        payload = self._build_payload(messages, tools, system, model, max_tokens)
        with self._open(payload) as resp:
            body = resp.read().decode("utf-8")

        return self.parse_response(json.loads(body))

    def complete_stream(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        model: str,
        max_tokens: int,
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> ModelResponse:
        payload = self._build_payload(messages, tools, system, model, max_tokens)
        payload["stream"] = True
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        raw_calls: Dict[int, Dict[str, str]] = {}
        finish_reason = ""
        chunks: List[JsonDict] = []
        with self._open(payload) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                chunks.append(chunk)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                content_delta = str(delta.get("content") or "")
                reasoning_delta = str(delta.get("reasoning_content") or "")
                if content_delta:
                    text_parts.append(content_delta)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                if on_delta and (content_delta or reasoning_delta):
                    on_delta(content_delta, reasoning_delta)
                for item in delta.get("tool_calls") or []:
                    index = int(item.get("index") or 0)
                    current = raw_calls.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if item.get("id"):
                        current["id"] = str(item["id"])
                    function = item.get("function") or {}
                    current["name"] += str(function.get("name") or "")
                    current["arguments"] += str(function.get("arguments") or "")
                finish_reason = str(choice.get("finish_reason") or finish_reason)
        calls = []
        for index in sorted(raw_calls):
            item = raw_calls[index]
            raw_arguments = item["arguments"] or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except ValueError:
                arguments = {"_raw": raw_arguments}
            calls.append(
                ToolCall(item["id"] or "call_%d" % (index + 1), item["name"], arguments)
            )
        return ModelResponse(
            text="".join(text_parts),
            reasoning_content="".join(reasoning_parts),
            tool_calls=calls,
            stop_reason="tool_use" if calls or finish_reason == "tool_calls" else "end_turn",
            raw={"stream_chunks": chunks[-20:]},
            usage=next(
                (
                    dict(chunk.get("usage") or {})
                    for chunk in reversed(chunks)
                    if chunk.get("usage")
                ),
                {},
            ),
        )

    def parse_response(self, payload: JsonDict) -> ModelResponse:
        choices = payload.get("choices") or []
        if not choices:
            return ModelResponse(text="", stop_reason="empty", raw=payload)

        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reasoning_content = message.get("reasoning_content") or ""
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
                    id=str(raw_call.get("id") or "call_%d" % (len(tool_calls) + 1)),
                    name=str(fn.get("name") or ""),
                    arguments=args,
                )
            )

        finish_reason = choice.get("finish_reason") or ""
        stop_reason = "tool_use" if tool_calls or finish_reason == "tool_calls" else "end_turn"
        return ModelResponse(
            text=text,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw=payload,
            usage=dict(payload.get("usage") or {}),
        )

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        return headers

    def _build_payload(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        model: str,
        max_tokens: int,
    ) -> Dict[str, Any]:
        self._enforce_context_budget(messages, tools, system, max_tokens)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
        }
        thinking = self._thinking_config(model)
        if thinking is not None:
            payload["thinking"] = thinking
        if tools:
            payload["tools"] = [self._to_openai_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        return payload

    def _enforce_context_budget(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        max_tokens: int,
    ) -> None:
        if not self.context_window:
            return
        budget = build_runtime_context_budget(
            self.context_window,
            self.effective_context_percent,
            max_tokens,
        )
        estimated = estimate_context_tokens(
            messages,
            tools,
            system_prompt=system,
        )
        if estimated > budget.input_budget:
            raise ContextLengthError(
                "Model context estimate exceeds budget: estimated=%d budget=%d quality=approximate"
                % (estimated, budget.input_budget)
            )

    def _open(self, payload: Dict[str, Any]):
        request = urllib.request.Request(
            self._chat_completions_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        for attempt in range(3):
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                lowered = detail.lower()
                if any(
                    marker in lowered
                    for marker in (
                        "context length",
                        "context_length",
                        "maximum context",
                        "too many tokens",
                    )
                ):
                    raise ContextLengthError(
                        "Model context is too long: HTTP %s %s" % (exc.code, detail)
                    ) from exc
                if any(
                    marker in lowered
                    for marker in ("content safety", "content_filter", "safety policy")
                ):
                    raise ContentSafetyError(
                        "Model content safety rejection: HTTP %s %s" % (exc.code, detail)
                    ) from exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = float(retry_after) if retry_after else 2**attempt
                    except ValueError:
                        delay = 2**attempt
                    time.sleep(min(10.0, max(0.1, delay)))
                    continue
                raise RuntimeError(
                    "Model request failed: HTTP %s %s" % (exc.code, detail)
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(
                    "Model request failed for %s: %s. Check OPENAI_COMPATIBLE_BASE_URL, "
                    "network/DNS/proxy settings, API key, and MODEL_ID."
                    % (self._chat_completions_url(), exc.reason)
                ) from exc
        raise RuntimeError("Model request failed after retries")

    def _chat_completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def _thinking_config(self, model: str) -> Optional[JsonDict]:
        requested = os.getenv("OPENAI_COMPATIBLE_THINKING")
        if requested:
            value = requested.strip().lower()
            if value in ("enabled", "disabled"):
                return {"type": value}
            if value in ("off", "false", "0", "no"):
                return {"type": "disabled"}
            if value in ("on", "true", "1", "yes"):
                return {"type": "enabled"}

        if self.thinking_enabled is not None:
            return {"type": "enabled" if self.thinking_enabled else "disabled"}

        if "api.deepseek.com" in self.base_url and model.startswith("deepseek-v4-"):
            return {"type": "disabled"}
        return None

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
                assistant_msg = {
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
                if msg.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = msg.get("reasoning_content")
                converted.append(assistant_msg)
                continue
            converted_msg = {"role": role, "content": msg.get("content", "")}
            if role == "assistant" and msg.get("reasoning_content"):
                converted_msg["reasoning_content"] = msg.get("reasoning_content")
            converted.append(converted_msg)
        return converted
