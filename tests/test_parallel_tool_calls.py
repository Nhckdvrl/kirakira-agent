"""并行工具调用的顺序不变量：执行可重叠，ToolResult 与回执仍按模型顺序落盘。

守护概念：SES-002/C4 —— 同一来源的事实追加顺序是权威语义，重叠执行不得改变它。
两条路径即够：正常提交按模型顺序落盘；前驱失败时已重叠的后继也不得抢先落盘。
这些用例在没有 `parallel` 注册与提交门的旧实现上失败（register/react 无对应参数）。
"""

import asyncio
from typing import cast

import pytest

from plugins.ledger.contract import ContentPart, Control, Input, Output, ToolResult
from plugins.models.contract import (
    LLMResponse,
)
from plugins.models.contract import (
    ToolCall as ModelToolCall,
)
from plugins.tools.execution import Result
from tests.support.message_react import runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("without_abort", [False, True])
async def test_step_limit_closes_settled_work_or_requires_explicit_source_control(tmp_path, without_abort):
    from plugins.react.plugin import STEP_LIMIT_ABANDON, StepLimit

    requests = []
    effects = []

    async def complete(request):
        requests.append(request)
        if len(requests) == 1:
            return LLMResponse(None, [ModelToolCall("only-call", "example", {})])
        return LLMResponse("new question answered")

    async def invoke(key, _arguments):
        effects.append(key)
        return Result("success", (ContentPart("text", "saved result"),))

    async with runtime(tmp_path, complete, invoke, max_steps=1,
                       without_abort=without_abort) as (conversation, log, _, run):
        await conversation.accept("first", Input((ContentPart("text", "original work"),)))
        first = await conversation.start(run)
        if without_abort:
            with pytest.raises(RuntimeError) as error:
                await first.join()
            assert isinstance(error.value.args[0], StepLimit)
            await conversation.accept("still-stuck", Input((ContentPart("text", "new request"),)))
            again = await conversation.start(run)
            with pytest.raises(RuntimeError):
                await again.join()
            assert len(requests) == 1
            head = log.reader("s").head(source="conversation")
            await conversation.control("manual-abandon", Control("abandon", head, "explicit user choice"),
                                       expected_head=head, handle=None)
        else:
            ended = await first.join()
            assert isinstance(ended.body, Control) and ended.body.action == "abandon"
            assert ended.body.reason.startswith(STEP_LIMIT_ABANDON)
        preserved = log.reader("s").snapshot()
        await conversation.accept("fresh", Input((ContentPart("text", "a fresh question"),)))
        second = await conversation.start(run)
        ended = await second.join()
        assert isinstance(ended.body, Output) and ended.body.finish == "complete"
        assert len(requests) == 2 and len(effects) == 1
        assert log.reader("s").snapshot()[:len(preserved)] == preserved


@pytest.mark.asyncio
async def test_step_limit_with_missing_receipt_preserves_unknown_effect(tmp_path):
    from plugins.react.plugin import STEP_LIMIT_FAILURE, StepLimit

    requests = []

    async def complete(request):
        requests.append(request)
        return LLMResponse(None, [ModelToolCall("unknown", "example", {})])

    async def invoke(_key, _arguments):
        raise AssertionError("the synthetic missing receipt must never invoke a real effect")

    async with runtime(tmp_path, complete, invoke, max_steps=1,
                       drop_receipt=lambda _: True) as (conversation, log, _, run):
        await conversation.accept("first", Input(()))
        task = await conversation.start(run)
        with pytest.raises(RuntimeError) as error:
            await task.join()
        assert isinstance(error.value.args[0], StepLimit)
        assert str(error.value.args[0]).startswith(STEP_LIMIT_FAILURE)
        assert "1 个工具调用" in str(error.value.args[0]) and "重试不会生效" in str(error.value.args[0])
        assert len(requests) == 1
        controls = [row.body for row in log.reader("s").snapshot() if isinstance(row.body, Control)]
        assert controls and all(body.action == "failure" for body in controls)
        assert not _tool_results(log)


def _tool_results(log):
    return [message.body for message in log.reader("s").snapshot() if isinstance(message.body, ToolResult)]


@pytest.mark.asyncio
async def test_parallel_calls_overlap_but_results_commit_in_model_order(tmp_path):
    """执行可重叠，但 ToolResult 仍按模型顺序落盘。"""
    entered: list[int] = []
    finished: list[int] = []
    tail_done = asyncio.Event()
    release_first = asyncio.Event()
    requests = []

    async def complete(request):
        requests.append(request)
        if len(requests) == 1:
            return LLMResponse(None, [ModelToolCall("a", "example", {"call": 0}),
                                      ModelToolCall("b", "example", {"call": 1}),
                                      ModelToolCall("c", "example", {"call": 2})])
        rows = [row for row in request.messages if row["role"] == "tool"]
        assert [row["tool_call_id"] for row in rows] == ["a", "b", "c"]
        return LLMResponse("done")

    async def invoke(key, arguments):
        index = cast(int, arguments["call"])
        entered.append(index)
        if index == 0:
            await release_first.wait()
        finished.append(index)
        if len(finished) >= 2 and 0 not in finished:
            tail_done.set()
        return Result("success", (ContentPart("text", str(index)),))

    async with runtime(tmp_path, complete, invoke, parallel_ids=frozenset({"tool"}),
                       max_parallel_calls=4) as (conversation, log, _, run):
        await conversation.accept("u1", Input(()))
        task = await conversation.start(run)
        await asyncio.wait_for(tail_done.wait(), 5)
        assert _tool_results(log) == []
        release_first.set()
        await asyncio.wait_for(task.join(), 5)
        assert [item.call_ref.part_index for item in _tool_results(log)] == [0, 1, 2]
        assert sorted(entered) == sorted(finished) == [0, 1, 2]


@pytest.mark.asyncio
async def test_parallel_failure_does_not_commit_later_results_first(tmp_path):
    """前驱在提交前失败时，已重叠的后继不能先写 ToolResult。"""
    entered: list[int] = []
    overlapped = asyncio.Event()

    async def complete(request):
        if not any(row["role"] == "tool" for row in request.messages):
            return LLMResponse(None, [ModelToolCall("a", "example", {"call": 0}),
                                      ModelToolCall("b", "example", {"call": 1}),
                                      ModelToolCall("c", "example", {"call": 2})])
        rows = [row for row in request.messages if row["role"] == "tool"]
        assert [row["tool_call_id"] for row in rows] == ["a", "b", "c"]
        return LLMResponse("done")

    async def invoke(key, arguments):
        index = cast(int, arguments["call"])
        entered.append(index)
        if index == 0:
            await overlapped.wait()
            raise RuntimeError("boom")
        if {1, 2} <= set(entered):
            overlapped.set()
        return Result("success", (ContentPart("text", str(index)),))

    async with runtime(tmp_path, complete, invoke, parallel_ids=frozenset({"tool"}),
                       max_parallel_calls=4) as (conversation, log, _, run):
        await conversation.accept("u1", Input(()))
        task = await conversation.start(run)
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(task.join(), 5)
        failed = _tool_results(log)
        assert [item.call_ref.part_index for item in failed] == [0, 1, 2]
        assert [item.outcome for item in failed] == ["error", "success", "success"]
        assert sorted(entered) == [0, 1, 2]
        assert await conversation.start(run) is None
