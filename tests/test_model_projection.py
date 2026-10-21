"""Model request views and usage projections preserve their source semantics."""

from plugins.ledger.contract import ContentReferences
from plugins.models.contract import (
    BoundModelDescriptor,
    LLMResponse,
    ModelRequest,
)
from plugins.models.projection import MessageProjection


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
