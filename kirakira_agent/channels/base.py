"""Shared channel helpers."""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from uuid import uuid4


class AttachmentStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _ensure_root(self) -> Path:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if os.access(self.root, os.W_OK):
                return self.root
        except Exception:
            pass
        fallback = Path("/tmp/kirakira_uploads")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    def write_bytes(self, data: bytes, *, prefix: str, suffix: str) -> Path:
        root = self._ensure_root()
        path = root / ("%s%s%s" % (prefix, uuid4().hex, suffix or ".bin"))
        path.write_bytes(data)
        return path


class MessageDeduper:
    def __init__(self, max_size: int = 500) -> None:
        self.max_size = max(1, max_size)
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def seen(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self.max_size:
            self._seen.discard(self._order.popleft())
        return False

