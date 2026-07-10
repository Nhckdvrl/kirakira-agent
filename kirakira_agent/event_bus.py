"""Lifecycle event bus with ordered interception and observer fanout."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Dict, List, TypeAlias, TypeVar, cast

logger = logging.getLogger(__name__)
E = TypeVar("E")
Handler: TypeAlias = Callable[[E], Awaitable[E | None] | E | None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[type[object], List[Handler[object]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def on(self, event_type: type[E], handler: Handler[E]) -> None:
        self._handlers.setdefault(cast(type[object], event_type), []).append(
            cast(Handler[object], handler)
        )

    def off(self, event_type: type[E], handler: Handler[E]) -> None:
        handlers = self._handlers.get(cast(type[object], event_type), [])
        candidate = cast(Handler[object], handler)
        if candidate in handlers:
            handlers.remove(candidate)
        if not handlers:
            self._handlers.pop(cast(type[object], event_type), None)

    async def emit(self, event: E) -> E:
        for raw_handler in self._handlers.get(cast(type[object], type(event)), []):
            handler = cast(Handler[E], raw_handler)
            result = handler(event)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                event = cast(E, result)
        return event

    async def observe(self, event: object) -> None:
        for handler in self._handlers.get(type(event), []):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("observer error for %s", type(event).__name__)

    async def fanout(self, event: object) -> None:
        handlers = list(self._handlers.get(type(event), []))
        if not handlers:
            return
        await asyncio.gather(*(self._run_observer(event, h) for h in handlers))

    def enqueue(self, event: object) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self.fanout(event), name="event:%s" % type(event).__name__
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_observer(self, event: object, handler: Handler[object]) -> None:
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("observer error for %s", type(event).__name__)
