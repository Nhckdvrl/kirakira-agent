"""Invoke model transports and settle streaming workers on cancellation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from typing import Any, List, Tuple

from bus.events_lifecycle import ModelAttemptStatus, StreamDeltaReady
from core.schema import JsonDict, ModelResponse
from plugins.execution.service import current_execution
from plugins.models.types import (
    ModelOutputTruncated,
    ModelRetriesExhausted,
    RetryableModelError,
)
from plugins.reply_program.program import current_program
from utils.async_tasks import cancel_and_drain


class ModelInvoker:
    def __init__(self, model_client, config, event_bus):
        self.model_client, self.config, self.event_bus = model_client, config, event_bus

    async def complete(
        self,
        messages,
        specs,
        **kwargs,
    ) -> ModelResponse:
        program = current_program(kwargs["session_key"])
        if program is None:
            return await self._complete(messages, specs, **kwargs)
        with program.model_scope():
            return await self._complete(messages, specs, **kwargs)

    async def _complete(
        self,
        messages,
        specs,
        **kwargs,
    ) -> ModelResponse:
        ctx = current_execution()
        if ctx:
            ctx.check()
            ctx.record(
                "model_request",
                {
                    "messages": messages,
                    "tools": [asdict(s) for s in specs],
                    "model": self.config.model,
                    "max_tokens": self.config.max_tokens,
                    **kwargs,
                },
            )
        frozen_messages, frozen_specs, frozen_options = deepcopy(
            (messages, specs, kwargs)
        )
        policy = self.config.retry_policy
        for attempt in range(1, policy.max_retries + 2):
            if ctx:
                ctx.check()
            try:
                result = await self._call(
                    deepcopy(frozen_messages),
                    deepcopy(frozen_specs),
                    **deepcopy(frozen_options),
                )
                if result.stop_reason in {"length", "max_tokens", "max_output_tokens"}:
                    raise ModelOutputTruncated(
                        "Model output was truncated; tool calls were not executed",
                        response=result,
                    )
            except (RetryableModelError, TimeoutError) as exc:
                delay = policy.delay(attempt, getattr(exc, "retry_after", None))
                exhausted = attempt > policy.max_retries
                status = ModelAttemptStatus(
                    **{
                        k: kwargs[k]
                        for k in ("session_key", "channel", "chat_id", "iteration")
                    },
                    attempt=attempt,
                    max_attempts=policy.max_retries + 1,
                    phase="exhausted" if exhausted else "retrying",
                    delay_seconds=0 if exhausted else delay,
                    reason=str(exc),
                )
                if ctx:
                    ctx.record("model_attempt_failed", asdict(status))
                await self.event_bus.fanout(
                    StreamDeltaReady(
                        **{
                            k: kwargs[k]
                            for k in ("session_key", "channel", "chat_id", "iteration")
                        },
                        reset=True,
                    )
                )
                await self.event_bus.fanout(status)
                if exhausted:
                    raise ModelRetriesExhausted(attempt, exc) from exc
                await asyncio.sleep(delay)
                continue
            if attempt > 1:
                await self.event_bus.fanout(
                    ModelAttemptStatus(
                        **{
                            k: kwargs[k]
                            for k in ("session_key", "channel", "chat_id", "iteration")
                        },
                        attempt=attempt,
                        max_attempts=policy.max_retries + 1,
                        phase="recovered",
                    )
                )
            break
        if ctx:
            ctx.record("model_response", asdict(result))
            ctx.check()
        return result

    async def _call(
        self,
        messages: List[JsonDict],
        specs: List[Any],
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        iteration: int,
        tool_choice: object | None = None,
    ) -> ModelResponse:
        options = {"tool_choice": tool_choice} if tool_choice is not None else {}
        timeout = max(1.0, float(self.config.model_timeout_seconds))
        stream_method = getattr(self.model_client, "complete_stream", None)
        acomplete = getattr(self.model_client, "acomplete", None)
        try:
            if not self.config.stream or not callable(stream_method):
                # 异步原生客户端直接 await；同步 stub 回退到 to_thread(complete)。
                if callable(acomplete):
                    call = acomplete(
                        messages,
                        specs,
                        "",
                        self.config.model,
                        self.config.max_tokens,
                        **options,
                    )
                else:
                    call = asyncio.to_thread(
                        self.model_client.complete,
                        messages,
                        specs,
                        "",
                        self.config.model,
                        self.config.max_tokens,
                        **options,
                    )
                worker = asyncio.create_task(call)
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(worker), timeout=timeout
                    )
                finally:
                    await cancel_and_drain(worker)
            queue: asyncio.Queue[Tuple[str, str] | None] = asyncio.Queue()
            loop = asyncio.get_running_loop()
            accepting_deltas = True

            def on_delta(content: str, reasoning: str) -> None:
                if accepting_deltas:
                    try:
                        loop.call_soon_threadsafe(
                            queue.put_nowait, (content, reasoning)
                        )
                    except RuntimeError:
                        pass  # A synchronous provider may outlive cancellation.

            astream = getattr(self.model_client, "acomplete_stream", None)
            if callable(astream):
                # 异步原生流:worker 直接在事件循环上跑,on_delta 从循环线程回投 queue。
                worker = asyncio.create_task(
                    astream(
                        messages,
                        specs,
                        "",
                        self.config.model,
                        self.config.max_tokens,
                        on_delta,
                        **options,
                    )
                )
            else:
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        stream_method,
                        messages,
                        specs,
                        "",
                        self.config.model,
                        self.config.max_tokens,
                        on_delta,
                        **options,
                    )
                )
            # Completion wakes the consumer even for a content-free response;
            # already scheduled deltas remain ahead of the completion sentinel.
            worker.add_done_callback(lambda _: queue.put_nowait(None))
            try:
                deadline = loop.time() + timeout
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        worker.cancel()
                        raise asyncio.TimeoutError
                    delta = await asyncio.wait_for(queue.get(), timeout=remaining)
                    if delta is None:
                        break
                    content_delta, reasoning_delta = delta
                    await self.event_bus.fanout(
                        StreamDeltaReady(
                            session_key=session_key,
                            channel=channel,
                            chat_id=chat_id,
                            iteration=iteration,
                            content_delta=content_delta,
                            reasoning_delta=reasoning_delta,
                        )
                    )
                return await worker
            finally:
                accepting_deltas = False
                await cancel_and_drain(worker)
        except asyncio.TimeoutError as exc:
            raise RetryableModelError("LLM 请求超过 %.1f 秒。" % timeout) from exc
