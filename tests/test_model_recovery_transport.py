"""Model generation recovery over real loopback HTTP/SSE boundaries."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import pytest
from agent.core.contracts import RuntimeConfig
from agent.core.execution import ExecutionStore
from agent.core.reasoner import DefaultReasoner
from agent.model_runtime.retry import ModelRetryPolicy, retry_after_seconds
from agent.model_runtime.types import ModelRetriesExhausted, ModelOutputTruncated
from agent.tools.registry import ToolRegistry
from bus.event_bus import EventBus
from bus.events_lifecycle import ModelAttemptStatus, StreamDeltaReady
from core.schema import ToolSpec
from infra.providers.llm_provider import OpenAICompatibleClient
from infra.providers.step_plan import StepPlanClient, validate_step_plan_url


@contextmanager
def server(responses):
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            bodies.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            status, content = responses[min(len(bodies) - 1, len(responses) - 1)]
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(content.encode())

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/v1", bodies
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def chunk(delta=None, finish=None):
    return (
        "data: "
        + json.dumps({"choices": [{"delta": delta or {}, "finish_reason": finish}]})
        + "\n\n"
    )


@pytest.mark.asyncio
async def test_completed_tool_is_not_replayed_when_http_and_stream_fail(tmp_path):
    tool = chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "c1",
                    "function": {"name": "write", "arguments": "{}"},
                }
            ]
        },
        "tool_calls",
    )
    stream = chunk({"content": "unfinished fragment"})
    success = chunk({"content": "done"}, "stop")
    with server([(200, tool), (503, "{}"), (200, stream), (200, success)]) as (
        url,
        bodies,
    ):
        bus = EventBus()
        events = []
        deltas = []
        bus.on(ModelAttemptStatus, events.append)
        bus.on(StreamDeltaReady, deltas.append)
        tools = ToolRegistry()
        writes = []

        def write():
            writes.append("written")
            return "written"

        tools.register(
            ToolSpec("write", "write once", {"type": "object", "properties": {}}), write
        )
        store = ExecutionStore(tmp_path / "runs.db")
        engine = DefaultReasoner(
            model_client=OpenAICompatibleClient(base_url=url, api_key="local-test"),
            tools=tools,
            context=None,
            event_bus=bus,
            execution_store=store,
            config=RuntimeConfig(
                model="fake",
                retry_policy=ModelRetryPolicy(max_retries=2, initial_delay=0),
            ),
        )
        result = await engine.run(
            [{"role": "user", "content": "write"}],
            session_key="test:1",
            channel="test",
            chat_id="1",
            request_text="write",
        )
        assert result.reply == "done"
        assert writes == ["written"]
        assert len(bodies) == 4 and bodies[1] == bodies[2] == bodies[3]
        assert [e.phase for e in events] == ["retrying", "retrying", "recovered"]
        assert len([d for d in deltas if d.reset]) == 2
        assert (
            len(
                [
                    e
                    for e in store.events(result.execution_id)
                    if e["kind"] == "tool_intent"
                ]
            )
            == 1
        )
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,error,attempts",
    [
        (503, "{}", ModelRetriesExhausted, 3),
        (401, "{}", RuntimeError, 1),
        (200, chunk({"content": "partial"}, "length"), ModelOutputTruncated, 1),
    ],
)
async def test_bounded_transport_and_nonretryable_failures(
    tmp_path, status, body, error, attempts
):
    from agent.core.model_io import ModelInvoker

    with server([(status, body)]) as (url, bodies):
        invoker = ModelInvoker(
            OpenAICompatibleClient(base_url=url, api_key="local-test"),
            RuntimeConfig(
                model="fake",
                retry_policy=ModelRetryPolicy(max_retries=2, initial_delay=0),
            ),
            EventBus(),
        )
        with pytest.raises(error):
            await invoker.complete(
                [], [], session_key="s", channel="c", chat_id="1", iteration=1
            )
        assert len(bodies) == attempts


def test_retry_after_and_plan_only_endpoint():
    assert retry_after_seconds("12") == 12
    assert retry_after_seconds("nan") is None
    assert ModelRetryPolicy().delay(1, 1234) == 1234
    for url in (
        "https://api.stepfun.com/v1",
        "http://api.stepfun.com/step_plan/v1",
        "https://api.stepfun.com.evil/step_plan/v1",
        "https://api.stepfun.com/step_plan/v1?x=1",
    ):
        with pytest.raises(ValueError):
            StepPlanClient(base_url=url, api_key="unused")
    assert (
        validate_step_plan_url("https://api.stepfun.com/step_plan/v1/")
        == "https://api.stepfun.com/step_plan/v1"
    )


@pytest.mark.asyncio
async def test_cancellation_interrupts_server_directed_backoff():
    from agent.core.model_io import ModelInvoker
    from agent.model_runtime.types import RetryableModelError

    class Client:
        calls = 0

        async def acomplete(self, *args, **kwargs):
            self.calls += 1
            raise RetryableModelError("busy", retry_after=3600)

    client = Client()
    bus = EventBus()
    waiting = asyncio.Event()
    bus.on(ModelAttemptStatus, lambda event: waiting.set())
    invoker = ModelInvoker(client, RuntimeConfig(model="fake", stream=False), bus)
    task = asyncio.create_task(
        invoker.complete([], [], session_key="s", channel="c", chat_id="1", iteration=1)
    )
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert client.calls == 1


def test_step5_uses_native_reasoning_effort_and_does_not_claim_thinking_is_off():
    client = StepPlanClient(
        api_key="unused", reasoning_effort="high", thinking_enabled=False
    )
    payload = client._build_payload([], [], "", "step5", 8192)
    assert payload["model"] == "step-5-preview"
    assert payload["reasoning_effort"] == "high" and "thinking" not in payload
    with pytest.raises(ValueError, match="reasoning_effort"):
        StepPlanClient(api_key="unused", reasoning_effort="disabled")


def test_truncated_stream_keeps_usage_without_parsing_incomplete_tool_arguments():
    client = OpenAICompatibleClient(base_url="http://unused", api_key="unused")
    state = client._new_stream_state()
    state.update(
        finish_reason="length",
        text_parts=["partial"],
        chunks=[{"usage": {"prompt_tokens": 7, "completion_tokens": 12}}],
        raw_calls={0: {"id": "c", "name": "write", "arguments": "{broken"}},
    )
    response = client._finalize_stream(state)
    assert response.stop_reason == "length" and response.tool_calls == []
    assert response.usage == {"prompt_tokens": 7, "completion_tokens": 12}
