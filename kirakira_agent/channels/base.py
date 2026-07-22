"""Shared channel helpers."""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from typing import Callable
from uuid import uuid4

from kirakira_agent.session import SessionManager

_MISSING_METADATA = object()


class AttachmentStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _resolve_root(self) -> Path:
        if self.root.is_symlink():
            raise ValueError(f"附件目录不能是符号链接: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError(f"附件目录不能是符号链接: {self.root}")
        if not os.access(self.root, os.W_OK):
            raise PermissionError(f"附件目录不可写: {self.root}")
        return self.root

    def create_path(self, prefix: str, suffix: str) -> Path:
        return self._resolve_root() / f"{prefix}{uuid4().hex}{suffix}"

    def create_persistent_path(self, prefix: str, suffix: str) -> Path:
        return self.create_path(prefix, suffix)

    def write_bytes(self, data: bytes, *, prefix: str, suffix: str) -> Path:
        path = self.create_path(prefix, suffix or ".bin")
        path.write_bytes(data)
        return path


class SessionIdentityIndex:
    """Maintain Reference's identity to chat_id session metadata index."""

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        channel: str,
        metadata_key: str,
        normalizer: Callable[[str], str] | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._channel = channel
        self._metadata_key = metadata_key
        self._normalizer = normalizer or (lambda value: value)
        self.mapping: dict[str, str] = {}

    def rebuild(self) -> dict[str, str]:
        self.mapping.clear()
        for entry in self._session_manager.list_sessions():
            key = str(entry.get("key") or "")
            if not key.startswith(f"{self._channel}:"):
                continue
            raw_value = entry.get("metadata", {}).get(self._metadata_key)
            if not isinstance(raw_value, str):
                continue
            normalized = self._normalize(raw_value)
            if normalized:
                self.mapping[normalized] = key.split(":", 1)[1]
        return dict(self.mapping)

    def resolve(self, identity: str) -> str | None:
        normalized = self._normalize(identity)
        return self.mapping.get(normalized) if normalized else None

    async def remember(self, identity: str, chat_id: str) -> None:
        normalized = self._normalize(identity)
        if not normalized:
            return
        session = self._session_manager.get_or_create(f"{self._channel}:{chat_id}")
        if session.metadata.get(self._metadata_key) == normalized:
            self.mapping[normalized] = chat_id
            return
        previous = session.metadata.get(self._metadata_key, _MISSING_METADATA)
        session.metadata[self._metadata_key] = normalized
        try:
            await self._session_manager.save_async(session)
        except BaseException:
            if previous is _MISSING_METADATA:
                session.metadata.pop(self._metadata_key, None)
            else:
                session.metadata[self._metadata_key] = previous
            raise
        self.mapping[normalized] = chat_id

    def _normalize(self, value: str) -> str:
        return self._normalizer((value or "").strip())


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
