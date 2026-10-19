"""A provider owns its tasks and resources through one physical close boundary."""

import asyncio
import inspect


class ResourceScope:
    def __init__(self):
        self._tasks = set()
        self._errors = []
        self._closers = []
        self._closed = False
        self._closing = None

    @property
    def tasks(self):
        return tuple(self._tasks)

    def on_close(self, closer):
        if self._closed:
            raise RuntimeError("resource scope is closed")
        self._closers.append(closer)

    def _settled(self, task):
        self._tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._errors.append(error)

    def spawn(self, coroutine, *, name=None):
        if self._closed:
            coroutine.close()
            raise RuntimeError("resource scope is closed")
        try:
            task = asyncio.create_task(coroutine, name=name)
        except BaseException:
            coroutine.close()
            raise
        self._tasks.add(task)
        task.add_done_callback(self._settled)
        return task

    async def stop_tasks(self):
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _close(self):
        await self.stop_tasks()
        errors, self._errors = self._errors, []
        failed = []
        for closer in reversed(self._closers):
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                failed.append(closer)
                errors.append(RuntimeError("resource closer cancelled during disposal"))
            except Exception as exc:  # noqa: BLE001 -- attempt every resource closer
                failed.append(closer)
                errors.append(exc)
        # Keep the original owner of failed physical disposal for an explicit retry.
        self._closers = list(reversed(failed))
        if errors:
            raise BaseExceptionGroup("provider resource cleanup failed", errors)

    async def close(self):
        if (
            self._closing is not None
            and self._closing.done()
            and self._closing.exception() is not None
        ):
            self._closing = None
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._close())
        # A cancelled caller still waits for physical disposal before reporting it.
        cancellation = None
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError as exc:
                cancellation = exc
        self._closing.result()
        if cancellation is not None:
            raise cancellation
