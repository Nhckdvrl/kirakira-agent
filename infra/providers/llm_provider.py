"""Kirakira Agent learning harness module."""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from core.schema import JsonDict, ModelResponse, ToolCall, ToolSpec
from plugins.models.context_policy import estimate_context_tokens
from plugins.models.json_utils import load_json_object_loose
from plugins.models.retry import retry_after_seconds
from plugins.models.types import (
    ContentSafetyError,
    ContextLengthError,
    RetryableModelError,
)

logger = logging.getLogger(__name__)


def _parse_tool_arguments(raw_arguments: str) -> JsonDict:
    """严格解析工具参数，并只修复仍可恢复为 JSON object 的文本。"""

    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        arguments = load_json_object_loose(raw_arguments)
        if arguments is None:
            raise
        logger.warning(
            "[llm] repaired malformed tool arguments original_chars=%d",
            len(raw_arguments),
        )
    if not isinstance(arguments, dict):
        raise TypeError("LLM 工具调用参数必须是 JSON 对象")
    return arguments


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
        self.base_url = (
            base_url or os.getenv("OPENAI_COMPATIBLE_BASE_URL") or ""
        ).rstrip("/")
        self.api_key = (
            api_key
            if api_key is not None
            else os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
        )
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
        tool_choice: Any = "auto",
    ) -> ModelResponse:
        payload = self._build_payload(
            messages, tools, system, model, max_tokens, tool_choice=tool_choice
        )
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
        tool_choice: Any = "auto",
    ) -> ModelResponse:
        payload = self._build_payload(
            messages, tools, system, model, max_tokens, tool_choice=tool_choice
        )
        payload["stream"] = True
        state = self._new_stream_state()
        with self._open(payload) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not self._consume_stream_line(line, state, on_delta):
                    break
        return self._finalize_stream(state)

    # SSE 流解析在同步 complete_stream 与异步 acomplete_stream 之间共用,保证两条路一致。
    def _new_stream_state(self) -> Dict[str, Any]:
        return {
            "text_parts": [],
            "reasoning_parts": [],
            "raw_calls": {},
            "finish_reason": "",
            "chunks": [],
        }

    def _consume_stream_line(
        self,
        line: str,
        state: Dict[str, Any],
        on_delta: Optional[Callable[[str, str], None]],
    ) -> bool:
        """处理一行(已 strip 的)SSE。返回 False 表示流结束([DONE])。"""
        if not line or line.startswith(":") or not line.startswith("data:"):
            return True
        data = line[5:].strip()
        if data == "[DONE]":
            return False
        try:
            chunk = json.loads(data)
        except ValueError:
            return True
        state["chunks"].append(chunk)
        choices = chunk.get("choices") or []
        if not choices:
            return True
        choice = choices[0]
        delta = choice.get("delta") or {}
        content_delta = str(delta.get("content") or "")
        reasoning_delta = str(delta.get("reasoning_content") or "")
        if content_delta:
            state["text_parts"].append(content_delta)
        if reasoning_delta:
            state["reasoning_parts"].append(reasoning_delta)
        if on_delta and (content_delta or reasoning_delta):
            on_delta(content_delta, reasoning_delta)
        for item in delta.get("tool_calls") or []:
            index = int(item.get("index") or 0)
            current = state["raw_calls"].setdefault(
                index, {"id": "", "name": "", "arguments": ""}
            )
            if item.get("id"):
                current["id"] = str(item["id"])
            function = item.get("function") or {}
            current["name"] += str(function.get("name") or "")
            current["arguments"] += str(function.get("arguments") or "")
        state["finish_reason"] = str(
            choice.get("finish_reason") or state["finish_reason"]
        )
        return True

    def _finalize_stream(self, state: Dict[str, Any]) -> ModelResponse:
        chunks = state["chunks"]
        finish_reason = state["finish_reason"]
        if not finish_reason:
            raise RetryableModelError(
                "Model stream ended without a terminal finish reason"
            )
        if finish_reason == "length":
            return ModelResponse(
                text="".join(state["text_parts"]),
                stop_reason="length",
                usage=next(
                    (
                        dict(chunk["usage"])
                        for chunk in reversed(chunks)
                        if chunk.get("usage")
                    ),
                    {},
                ),
            )
        calls = []
        for index in sorted(state["raw_calls"]):
            item = state["raw_calls"][index]
            raw_arguments = item["arguments"] or "{}"
            arguments = _parse_tool_arguments(raw_arguments)
            calls.append(
                ToolCall(item["id"] or "call_%d" % (index + 1), item["name"], arguments)
            )
        return ModelResponse(
            text="".join(state["text_parts"]),
            reasoning_content="".join(state["reasoning_parts"]),
            tool_calls=calls,
            stop_reason="length"
            if finish_reason == "length"
            else ("tool_use" if calls or finish_reason == "tool_calls" else "end_turn"),
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

    async def acomplete_stream(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        model: str,
        max_tokens: int,
        on_delta: Optional[Callable[[str, str], None]] = None,
        tool_choice: Any = "auto",
    ) -> ModelResponse:
        import httpx

        payload = self._build_payload(
            messages, tools, system, model, max_tokens, tool_choice=tool_choice
        )
        payload["stream"] = True
        url = self._chat_completions_url()
        headers = self._headers()
        content = json.dumps(payload).encode("utf-8")
        state = self._new_stream_state()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                async with client.stream(
                    "POST", url, content=content, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode(
                            "utf-8", errors="replace"
                        )
                        raise self._http_error_for(
                            response.status_code, detail
                        ) or RetryableModelError(
                            "Model request failed: HTTP %s %s"
                            % (response.status_code, detail),
                            status_code=response.status_code,
                            retry_after=retry_after_seconds(
                                response.headers.get("Retry-After")
                            ),
                        )
                    async for raw_line in response.aiter_lines():
                        if not self._consume_stream_line(
                            raw_line.strip(), state, on_delta
                        ):
                            break
        except httpx.RequestError as exc:
            raise RetryableModelError(
                f"Model stream transport failed: {type(exc).__name__}"
            ) from exc
        return self._finalize_stream(state)

    # --- async-native transport (Phase 1): 直接 await，去掉 to_thread 阻抗 ---

    async def acomplete(
        self,
        messages: List[JsonDict],
        tools: List[ToolSpec],
        system: str,
        model: str,
        max_tokens: int,
        tool_choice: Any = "auto",
    ) -> ModelResponse:
        payload = self._build_payload(
            messages, tools, system, model, max_tokens, tool_choice=tool_choice
        )
        body = await self._arequest(payload)
        return self.parse_response(json.loads(body))

    def _http_error_for(self, code: int, detail: str) -> Optional[Exception]:
        """分类 HTTP 错误。返回 None 表示该状态码可重试(429/5xx),否则返回要抛的异常。"""
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
            return ContextLengthError(
                "Model context is too long: HTTP %s %s" % (code, detail)
            )
        if any(
            marker in lowered
            for marker in ("content safety", "content_filter", "safety policy")
        ):
            return ContentSafetyError(
                "Model content safety rejection: HTTP %s %s" % (code, detail)
            )
        if code in (429, 500, 502, 503, 504):
            return None
        return RuntimeError("Model request failed: HTTP %s %s" % (code, detail))

    async def _arequest(self, payload: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=False
        ) as client:
            try:
                response = await client.post(
                    self._chat_completions_url(),
                    content=json.dumps(payload).encode("utf-8"),
                    headers=self._headers(),
                )
            except httpx.RequestError as exc:
                raise RetryableModelError(
                    f"Model transport failed: {type(exc).__name__}"
                ) from exc
            if response.status_code >= 400:
                raise self._http_error_for(
                    response.status_code, response.text
                ) or RetryableModelError(
                    f"Model request failed: HTTP {response.status_code} {response.text}",
                    status_code=response.status_code,
                    retry_after=retry_after_seconds(
                        response.headers.get("Retry-After")
                    ),
                )
            return response.text

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
                args = _parse_tool_arguments(raw_args) if raw_args.strip() else {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                raise TypeError("LLM 工具调用参数必须是 JSON 对象")
            tool_calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or "call_%d" % (len(tool_calls) + 1)),
                    name=str(fn.get("name") or ""),
                    arguments=args,
                )
            )

        finish_reason = choice.get("finish_reason") or ""
        stop_reason = (
            "length"
            if finish_reason == "length"
            else (
                "tool_use"
                if tool_calls or finish_reason == "tool_calls"
                else "end_turn"
            )
        )
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
        tool_choice: Any = "auto",
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
            # 照 Reference provider.chat:调用方可要求 "required" 或具名强制
            # {"type":"function","function":{"name":...}} —— Drift 收尾与主动判定
            # 靠它拿到服务端保证的工具调用,而不是靠 prompt 约束。
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
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
        input_budget = self.context_window - max_tokens
        estimated = estimate_context_tokens(
            messages,
            tools,
            system_prompt=system,
        )
        if estimated > input_budget:
            raise ContextLengthError(
                "Model context estimate exceeds budget: estimated=%d budget=%d quality=approximate"
                % (estimated, input_budget)
            )

    def _open(self, payload: Dict[str, Any]):
        request = urllib.request.Request(
            self._chat_completions_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise self._http_error_for(exc.code, detail) or RetryableModelError(
                f"Model request failed: HTTP {exc.code} {detail}",
                status_code=exc.code,
                retry_after=retry_after_seconds(
                    exc.headers.get("Retry-After") if exc.headers else None
                ),
            ) from exc
        except urllib.error.URLError as exc:
            raise RetryableModelError(f"Model transport failed: {exc.reason}") from exc

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

    def _to_openai_messages(
        self, messages: List[JsonDict], system: str
    ) -> List[JsonDict]:
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
                                "arguments": json.dumps(
                                    (call.get("function") or {}).get("arguments", {})
                                ),
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
