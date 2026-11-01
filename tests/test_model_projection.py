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


def test_historical_reminder_projection_preserves_raw_facts_and_tool_protocol():
    import hashlib
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from plugins.ledger.contract import (
        CallRef,
        ContentPart,
        Input,
        Message,
        Output,
        ToolCall,
        ToolResult,
    )
    from plugins.models.contract import ModelContinuation
    from plugins.models.contract import ToolCall as ModelToolCall
    from plugins.models.projection import response_facts

    original = "PRESERVE_THIS\nLARGE_AUTO_RECALL\nPRESERVE_THAT"
    opaque = ModelContinuation("fixture", {"cursor": "fixture-cursor"})
    response = LLMResponse("answer", (ModelToolCall("call-1", "tool", {}),), thinking="INTERNAL_REASONING",
                           call_record_id="record", continuation=opaque)
    facts = response_facts(response, (1,), reminder=original, reminder_input_id="old-input")
    def message(identity, seq, body):
        return Message(identity, "session", seq, datetime.now(UTC), "fixture", "conversation", body)
    rows = (
        message("old-input", 0, Input((ContentPart("text", "old user request"),))),
        message("caller", 1, Output((ContentPart("text", "answer"), ToolCall("tool-binding", {}), facts), "continue")),
        message("result", 2, ToolResult(CallRef("caller", 1), "success", (ContentPart("text", "TOOL_EVIDENCE"),))),
        message("completed", 3, Output((ContentPart("text", "completed answer"),), "complete")),
        message("new-input", 4, Input((ContentPart("text", "new user request"),))),
    )
    projection = MessageProjection(SimpleNamespace(descriptor=SimpleNamespace(binding_id="fixture")),
        source="conversation", render_content=lambda part: ({"type": "text", "text": part.value},) if part.kind == "text" else (),
        tool_name=lambda _: "tool", read_call=lambda _: {"state": "success", "binding": {"binding_id": "fixture"}},
        check_summary=lambda _: ContentReferences())
    baseline = projection.render(rows, after_seq=-1)
    start = original.index("LARGE_AUTO_RECALL")
    patch = {"message_id": "caller", "sha256": hashlib.sha256(original.encode()).hexdigest(),
             "start": start, "end": start + len("LARGE_AUTO_RECALL"), "text": "receipt:exact-source"}
    changed = projection.render(rows, after_seq=-1, reminder_patches=(patch,))
    assert changed.content_transformed and changed.continuation is None
    assert "LARGE_AUTO_RECALL" not in str(changed.messages)
    assert "PRESERVE_THIS" in str(changed.messages) and "PRESERVE_THAT" in str(changed.messages)
    assert next(row for row in changed.messages if row["role"] == "assistant")["reasoning_content"] == "INTERNAL_REASONING"
    assert next(row for row in changed.messages if row["role"] == "tool")["tool_call_id"] == "call-1"
    assert facts.value["reminder"] == original and rows[-1].body.parts[0].value == "new user request"
    assert projection.render(rows, after_seq=-1).messages == baseline.messages  # Cached view can restore originals.
    with pytest.raises(ValueError, match="校验失败"):
        projection.render(rows, after_seq=-1, reminder_patches=({**patch, "sha256": "0" * 64},))
    with pytest.raises(ValueError, match="校验失败"):
        projection.render(rows, after_seq=-1, reminder_patches=(patch, patch))


def test_sparse_summary_coverage_preserves_holes_current_input_and_cross_source_tool_facts():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from plugins.context.plugin import ContextBuilder
    from plugins.ledger.contract import (
        CallRef,
        ContentPart,
        Input,
        Message,
        Output,
        ToolCall,
        ToolResult,
    )
    from plugins.models.contract import ToolCall as ModelToolCall
    from plugins.models.projection import response_facts
    from plugins.reply_program.program import RequestHistory

    def message(identity, seq, source, body):
        return Message(identity, "session", seq, datetime.now(UTC), "fixture", source, body)

    model = SimpleNamespace(descriptor=SimpleNamespace(binding_id="fixture", capabilities=SimpleNamespace(context_window=None)),
        max_tool_schemas=None, estimate_context_tokens=lambda messages, tools: len(str(messages)))
    def project(source, keep=()):
        return MessageProjection(model, source=source, keep_input_ids=keep,
            render_content=lambda part: ({"type": "text", "text": part.value},) if part.kind == "text" else (),
            tool_name=lambda _: "tool", read_call=lambda _: {"state": "success", "binding": {"binding_id": "fixture"}},
            check_summary=lambda _: ContentReferences())

    rows = (
        message("old-hole", 0, "conversation", Input((ContentPart("text", "OLD_RAW_HOLE"),))),
        message("covered-input", 1, "conversation", Input((ContentPart("text", "COVERED_REQUEST"),))),
        message("covered-output", 2, "conversation", Output((ContentPart("text", "COVERED_ANSWER"),), "complete")),
        message("current", 3, "conversation", Input((ContentPart("text", "CURRENT_REQUEST"),))),
    )
    projection = project("conversation", ("current",))
    covered = frozenset(("covered-input", "covered-output"))
    raw_hole = projection.render(rows, after_seq=-1, covered_message_ids=covered)
    assert "OLD_RAW_HOLE" in str(raw_hole.messages) and "COVERED_ANSWER" not in str(raw_hole.messages)
    advanced = projection.render(rows, after_seq=-1, covered_message_ids=covered | {"old-hole", "current"})
    assert "OLD_RAW_HOLE" not in str(advanced.messages) and "CURRENT_REQUEST" in str(advanced.messages)
    assert projection.render(rows, after_seq=-1, covered_message_ids=covered).messages == raw_hole.messages

    facts = response_facts(LLMResponse(None, (ModelToolCall("call", "tool", {}),), call_record_id="record"), (0,))
    cross_source = (
        message("alpha-input", 0, "alpha", Input((ContentPart("text", "ALPHA_RAW_REQUEST"),))),
        message("alpha-call", 1, "alpha", Output((ToolCall("binding", {}), facts), "continue")),
        message("alpha-result", 2, "alpha", ToolResult(CallRef("alpha-call", 0), "success", (ContentPart("text", "ALPHA_RAW_RESULT"),))),
        message("beta-input", 3, "beta", Input((ContentPart("text", "BETA_CURRENT_REQUEST"),))),
    )
    context = ContextBuilder()
    request_history = RequestHistory(context, "beta", own=False)
    summary = {"reference": "fixed-summary", "content": "saved facts",
               "source_message_ids": ("alpha-input", "alpha-call", "alpha-result")}
    request = request_history.build(cross_source, materials={"summary": summary}, model=project("beta", ("beta-input",)), max_output_tokens=0)
    assert "BETA_CURRENT_REQUEST" in str(request.messages) and "saved facts" in str(request.messages)
    assert "ALPHA_RAW" not in str(request.messages)
    for ids in (("alpha-input", "alpha-call"), ("alpha-input", "alpha-result")):
        with pytest.raises(ValueError, match="切开工具调用与结果"):
            context.build(cross_source, materials={"summary": {**summary, "source_message_ids": ids}},
                          model=project("beta"), max_output_tokens=0)


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


@pytest.fixture
def registry_store(tmp_path):
    from plugins.models.contract import CapabilitySources, ModelCapabilities
    from plugins.models.settings import AddConnection, AddModel, SetDefaultModel
    from plugins.models.store import MODEL_ROLES, ModelsStore

    store = ModelsStore(tmp_path / "models.sqlite3", tmp_path / "backups")
    try:
        store.initialize()
        revision = store.add_connection(AddConnection(
            0, "fixture", "Fixture", "fixture", "http://127.0.0.1:1/v1", "fixture",
            {"api_key": "synthetic-not-a-credential"}))
        for number in range(2):
            revision = store.add_model(AddModel(
                revision, f"model-{number}", "fixture", "chat", f"fixture-{number}",
                ModelCapabilities(context_window=32768, max_output_tokens=4096,
                                  input_modalities=("text", "image")), CapabilitySources()))
        for role in MODEL_ROLES:
            revision = store.set_default(SetDefaultModel(revision, role, "model-0"))
        yield store, revision
    finally:
        store.close()


def test_registry_snapshot_reuses_catalog_and_observes_configuration_changes(registry_store):
    import sqlite3
    from contextlib import closing

    from plugins.models.settings import SetDefaultModel, UpdateModel

    store, revision = registry_store
    initial = store.read_snapshot()
    assert initial.role_bindings == {role: "model-0" for role in ("default", "fast", "agent", "vision")}
    assert store.read_snapshot() is initial
    descriptor = BoundModelDescriptor(
        "binding", "plugin-snapshot", revision, "model-0", "fixture", "fixture", "1", "fixture",
        "fixture-0", "default", None, initial.models["model-0"].capabilities,
        initial.models["model-0"].capability_sources, "capability-digest")
    call_id = store.start_call(descriptor, ModelRequest(({"role": "user", "content": "synthetic"},)))
    store.finish_call(call_id, usage=None, failure=None, response=LLMResponse("synthetic response"))
    assert store.read_call(call_id)["state"] == "success"
    assert store.read_snapshot() is initial  # Call bookkeeping never rebuilds an unchanged catalog.

    revision = store.set_default(SetDefaultModel(revision, "default", "model-1"))
    selected = store.read_snapshot()
    assert selected is not initial and selected.revision == revision
    assert selected.role_bindings == {**initial.role_bindings, "default": "model-1"}
    revision = store.update_model(UpdateModel(revision, "model-1", 65536, 8192, True))
    configured = store.read_snapshot()
    assert configured.revision == revision
    assert configured.models["model-1"].capabilities.context_window == 65536
    assert configured.models["model-1"].capabilities.max_output_tokens == 8192

    # An external commit without a registry revision still invalidates the connection-local marker.
    with closing(sqlite3.connect(store.path)) as external, external:
        external.execute("UPDATE model_connections SET name='Externally updated' WHERE id='fixture'")
    external_snapshot = store.read_snapshot()
    assert external_snapshot.revision == revision
    assert external_snapshot.connections["fixture"].name == "Externally updated"
    assert configured.connections["fixture"].name == "Fixture"  # Previously bound snapshots stay immutable.
    assert store.read_snapshot() is external_snapshot


def test_registry_snapshot_busy_writer_uses_committed_reader_and_rechecks_schema(registry_store):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing

    store, revision = registry_store
    initial = store.read_snapshot()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store._connect() as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE model_role_bindings SET model_id='model-1' WHERE role='default'")
            writer.execute("UPDATE model_registry_meta SET revision=revision+1 WHERE singleton=1")
            committed = executor.submit(store.read_snapshot).result(timeout=3)
            assert committed.revision == revision
            assert committed.role_bindings == initial.role_bindings
            writer.commit()
    updated = store.read_snapshot()
    assert updated.revision == revision + 1 and updated.role_bindings["default"] == "model-1"
    assert updated.role_bindings["fast"] == "model-0"

    epoch = store.host_epoch
    store.initialize()  # Same-host initialization can perform additive upgrades without changing meta.
    assert store.host_epoch == epoch
    assert store.read_snapshot() == updated and store.read_snapshot() is not updated
    with closing(sqlite3.connect(store.path)) as external, external:
        external.execute("DROP TABLE model_definitions")
    with pytest.raises(RuntimeError, match="schema is incomplete.*model_definitions"):
        store.read_snapshot()
