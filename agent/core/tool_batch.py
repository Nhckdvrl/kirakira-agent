"""Bounded independent reads; barriers and model result order stay deterministic."""

import asyncio

from agent.core.execution import borrow_execution, current_execution
from agent.plugins.snapshot import (
    bind_runtime_snapshot,
    get_current_runtime_lease,
    reset_runtime_snapshot,
)


async def ordered_tool_results(calls, tools, invoke, *, limit=4, serial_names=()):
    """Only consecutive, explicitly safe reads may overlap.

    Unmarked tools, writes, searches and terminal calls are barriers. The parent
    retains session/plugin ownership until every child has physically finished.
    Results enter the next model request in the original call order.
    """
    execution, lease = current_execution(), get_current_runtime_lease()
    limit = max(1, int(limit))
    slots = asyncio.Semaphore(limit)

    def parallel(call):
        tool = tools.get_tool(call.name)
        return (
            limit > 1
            and call.name not in serial_names
            and tool is not None
            and tool.meta.risk == "read-only"
            and tool.meta.parallel_safe
        )

    async def owned(call):
        async with slots:
            return await bound(call)

    async def bound(call):
        with borrow_execution(execution):
            if lease is None:
                return await invoke(call)
            child = lease.fork()
            token = bind_runtime_snapshot(child)
            try:
                return await invoke(call)
            finally:
                reset_runtime_snapshot(token)
                await child.release()

    index = 0
    while index < len(calls):
        end = index + 1
        if parallel(calls[index]):
            while end < len(calls) and parallel(calls[end]):
                end += 1
        batch = calls[index:end]
        if len(batch) == 1:
            yield batch[0], await invoke(batch[0])
        else:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(owned(call), name=f"tool:{call.name}:{call.id}")
                    for call in batch
                ]
            for call, task in zip(batch, tasks, strict=True):
                yield call, task.result()
        index = end
