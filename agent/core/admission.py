"""One conversation admission boundary for channels, direct and control turns."""

import asyncio
from contextlib import asynccontextmanager


class TurnAdmission:
    def __init__(self, sessions):
        self.sessions = sessions
        self._entries = {}
        self._active = {}

    @asynccontextmanager
    async def hold(self, key):
        entry = self._entries.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
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

    def is_busy(self, key):
        return key in self._entries

    def interrupt(self, key):
        task = self._active.get(key)
        if task is None or task.done():
            return False
        task.cancel()
        return True
