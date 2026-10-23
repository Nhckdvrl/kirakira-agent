"""Model request views and usage projections preserve their source semantics."""

import json
from contextlib import aclosing

import httpx
import pytest

from plugins.ledger.contract import ContentReferences
from plugins.models.contract import (
    BoundModelDescriptor,
    LLMResponse,
    ModelRequest,
)
from plugins.models.projection import MessageProjection


@pytest.mark.parametrize("thinking_format", ["none", "deepseek"])
def test_internal_request_keeps_effort_unless_connection_can_disable_reasoning(thinking_format):
    from types import SimpleNamespace
    from plugins.openai_compatible.driver import _chat_body

    # No model-name/provider-name dispatch: the connection's negotiated protocol
    # decides whether an actual off switch exists.
    descriptor = SimpleNamespace(model="another-vendor-model", reasoning_effort="low")
    connection = SimpleNamespace(thinking_format=thinking_format)
    request = ModelRequest(({"role": "user", "content": "summarize"},),
                           max_output_tokens=1024, disable_reasoning=True)
    body = _chat_body(descriptor, connection, request)
    assert body["max_tokens"] == 1024
    if thinking_format == "none":
        assert body["reasoning_effort"] == "low"
        assert "thinking" not in body
    else:
        assert body["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in body


@pytest.mark.asyncio
async def test_streamed_tool_identity_is_stable_while_arguments_accumulate():
    from plugins.openai_compatible.driver import _consume_stream, _merge_tool_deltas

    def call(index, identity, name, args):
        return {"index": index, "id": identity, "function": {"name": name, "arguments": args}}

    chunks = [
        [call(0, "call-1", "read_memory_source", '{"message_id":')],
        [call(0, "call-1", "read_memory_source", '"source",'), call(1, "call-2", "other_tool", '{"value":')],
        [call(1, "call-2", "other_tool", '2}'), call(0, "call-1", "read_memory_source", '"offset":0}')],
    ]

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                yield ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": chunk}}]}) + "\n\n").encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'

    async with aclosing(httpx.Response(200, stream=Stream())) as response:
        result = await _consume_stream(response, None)
    assert [(c.id, c.name, dict(c.arguments)) for c in result.tool_calls] == [
        ("call-1", "read_memory_source", {"message_id": "source", "offset": 0}),
        ("call-2", "other_tool", {"value": 2}),
    ]
    saved = {}
    assert _merge_tool_deltas(saved, [call(0, "call-1", "read_memory_source", "")])
    assert not _merge_tool_deltas(saved, [call(0, "call-1", "read_memory_source", "")])
    with pytest.raises(RuntimeError, match="id changed"):
        _merge_tool_deltas(saved, [call(0, "another-call", "read_memory_source", "")])


class _CountingModel:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def descriptor(self) -> BoundModelDescriptor:
        raise AssertionError("估算测试不读取模型描述")

    async def complete(self, request: ModelRequest) -> LLMResponse:
        raise AssertionError("估算测试不调用模型")

    def estimate_appended_message_tokens(self, messages) -> int:
        raise AssertionError("估算测试不计算增量")

    @property
    def max_tool_schemas(self) -> int | None:
        raise AssertionError("估算测试不读取工具上限")

    def key_recovery(self, request_key: str) -> str:
        raise AssertionError("估算测试不读取恢复状态")

    def estimate_context_tokens(self, messages, tools=()):
        self.calls += 1
        return 42


def _projection(model: _CountingModel) -> MessageProjection:
    return MessageProjection(
        model,
        source="conversation",
        render_content=lambda part: (),
        tool_name=lambda binding: binding,
        read_call=lambda call_id: {},
        check_summary=lambda part: ContentReferences(),
    )


def test_estimate_reuses_result_for_same_request_object() -> None:
    model = _CountingModel()
    projection = _projection(model)
    request = ModelRequest(messages=[{"role": "user", "content": "hi"}])
    assert projection.estimate(request) == 42
    assert projection.estimate(request) == 42
    assert model.calls == 1
    # 新请求对象不命中身份备忘，重新估算。
    other = ModelRequest(messages=[{"role": "user", "content": "again"}])
    assert projection.estimate(other) == 42
    assert model.calls == 2


def test_cache_usage_preserves_unknown_zero_and_partial_turns_across_restart(tmp_path):
    import sqlite3

    from plugins.models.usage import _Checkpoint, _Fact, _record, _SourceState
    from plugins.turn_projection.contract import Turn

    state = _SourceState()
    # A Turn is unknown if even one call omits its cache count, despite exact
    # input/output usage. Explicit zero is an observed miss, not missing data.
    for index, counts in enumerate(((50, None), (0, 0), (40, 60))):
        ids = tuple(f"output-{index}-{n}" for n in range(2))
        facts = {identity: _Fact("2026-10-21T12:00:00+00:00", "", identity, True) for identity in ids}
        usages = {identity: {"coverage": "exact", "input_tokens": 100,
                            "output_tokens": 10, "cached_input_tokens": hit}
                  for identity, hit in zip(ids, counts, strict=True)}
        turn = Turn("conversation", index * 2 - 1, index * 2 + 1, ids[-1], "complete", ids, ())
        _record(state, "session", "conversation", turn, facts, usages)
    assert [turn.hit_tokens for turn in state.recent] == [None, 0, 100]
    assert state.totals.body() == {"turns": 3, "prompt_tokens": 600, "hit_tokens": 100,
                                   "cache_observed_turns": 2, "cache_observed_prompt_tokens": 400}
    assert len(state.outputs) == 3  # Missing cache metadata does not hide known output usage.
    checkpoint = _Checkpoint(tmp_path / "cache-usage.sqlite3")
    checkpoint.save({"session": 5}, {("session", "conversation"): state}, ())
    heads, restored = checkpoint.load()
    assert heads == {"session": 5}
    restored_state = restored[("session", "conversation")]
    assert restored_state.totals == state.totals
    assert restored_state.recent == state.recent and restored_state.outputs == state.outputs
    # Old aggregates included unknown counts as zero. They must be rebuilt,
    # rather than relabelled as fully observed after the new code starts.
    with sqlite3.connect(tmp_path / "cache-usage.sqlite3") as connection:
        connection.execute("PRAGMA user_version = 1")
    assert checkpoint.load() == ({}, {})
