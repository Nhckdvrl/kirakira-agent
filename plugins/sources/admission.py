"""One conversation admission boundary for channels, direct and control turns."""

import asyncio
from contextlib import asynccontextmanager

from agent.composition.scope import ResourceScope


class TurnAdmission:
    def __init__(self, sessions):
        self.sessions = sessions
        self._entries = {}
        self._active = {}
        self._participants = {}
        self._closed = False
        self._scope = ResourceScope()
        self._scope.on_close(self._drain)

    @asynccontextmanager
    async def hold(self, key):
        if self._closed:
            raise RuntimeError("conversation admission is closed")
        task = asyncio.current_task()
        self._participants[task] = self._participants.get(task, 0) + 1
        entry = self._entries.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                if self._closed:
                    raise RuntimeError("conversation admission is closed")
                owner = self.sessions.acquire_admission(key)
                self._active[key] = asyncio.current_task()
                try:
                    yield
                finally:
                    self._active.pop(key, None)
                    self.sessions.release_admission(key, owner)
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._entries.pop(key, None)
            self._participants[task] -= 1
            if not self._participants[task]:
                self._participants.pop(task)

    async def _drain(self):
        tasks = tuple(self._participants)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if asyncio.current_task() in self._participants:
            raise RuntimeError("cannot close admission from an admitted turn")
        self._closed = True
        await self._scope.close()

    def is_busy(self, key):
        return key in self._entries

    def interrupt(self, key):
        task = self._active.get(key)
        if task is None or task.done():
            return False
        task.cancel()
        return True
