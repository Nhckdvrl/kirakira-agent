"""Persistent chat sessions with tool-chain aware history reconstruction."""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import re
import sqlite3
import threading
from uuid import uuid4
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List

JsonDict = Dict[str, Any]
logger = logging.getLogger(__name__)


def _safe_key(key: str) -> str:
    readable = re.sub(r"[^\w.-]", "_", key).strip("._") or "session"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return "%s--%s" % (readable[:96], digest)


def _legacy_safe_key(key: str) -> str:
    return re.sub(r"[^\w.-]", "_", key)


def _truncate_tool_result(value: object, limit: int = 10000) -> str:
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    omitted = max(0, len(text) - limit)
    while True:
        marker = "…%d chars truncated…" % omitted
        keep = max(0, limit - len(marker))
        actual = len(text) - keep
        if actual == omitted:
            break
        omitted = actual
    head = keep // 2
    tail = keep - head
    clipped = text[:head] + marker + (text[-tail:] if tail else "")
    return "Total output lines: %d\n\n%s" % (len(text.splitlines()), clipped)


@dataclass
class Session:
    key: str
    messages: List[JsonDict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    metadata: JsonDict = field(default_factory=dict)
    last_consolidated: int = 0

    def add_message(
        self,
        role: str,
        content: str,
        *,
        media: List[str] | None = None,
        **kwargs: Any,
    ) -> None:
        msg: JsonDict = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().astimezone().isoformat(),
            **kwargs,
        }
        if media:
            msg["media"] = list(media)
        self.messages.append(msg)
        self.updated_at = datetime.now().astimezone().isoformat()

    def get_history(
        self,
        max_messages: int = 80,
        *,
        start_index: int | None = None,
    ) -> List[JsonDict]:
        if max_messages <= 0:
            selected = []
        elif start_index is not None:
            start = max(0, min(int(start_index), len(self.messages)))
            if start >= len(self.messages):
                return []
            while start > 0 and self.messages[start].get("role") != "user":
                start -= 1
            selected = self.messages[start:]
        else:
            selected = self.messages[-max_messages:]
        first_user = next(
            (index for index, msg in enumerate(selected) if msg.get("role") == "user"),
            None,
        )
        if first_user is None:
            return []
        selected = selected[first_user:]
        out: List[JsonDict] = []
        for msg in selected:
            role = msg.get("role")
            if role == "user":
                out.append({"role": "user", "content": msg.get("content", "")})
                continue
            if role != "assistant":
                continue
            for group in msg.get("tool_chain") or []:
                calls = list(group.get("calls") or [])
                if not calls:
                    continue
                assistant_tool_message = {
                    "role": "assistant",
                    "content": group.get("text") or "",
                    "tool_calls": [
                            {
                                "id": call["call_id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": call.get("arguments", {}),
                                },
                            }
                            for call in calls
                        ],
                }
                if group.get("reasoning_content"):
                    assistant_tool_message["reasoning_content"] = group[
                        "reasoning_content"
                    ]
                out.append(assistant_tool_message)
                for call in calls:
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["call_id"],
                            "content": _truncate_tool_result(call.get("result", "")),
                        }
                    )
            if msg.get("content"):
                assistant_msg = {"role": "assistant", "content": msg.get("content", "")}
                reasoning = msg.get("reasoning_content") or msg.get("thinking")
                if reasoning:
                    assistant_msg["reasoning_content"] = reasoning
                out.append(assistant_msg)
        return out

    def to_json(self) -> JsonDict:
        return {
            "key": self.key,
            "messages": self.messages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "last_consolidated": self.last_consolidated,
        }

    @classmethod
    def from_json(cls, data: JsonDict) -> "Session":
        return cls(
            key=str(data.get("key") or ""),
            messages=list(data.get("messages") or []),
            created_at=str(data.get("created_at") or datetime.now().astimezone().isoformat()),
            updated_at=str(data.get("updated_at") or datetime.now().astimezone().isoformat()),
            metadata=dict(data.get("metadata") or {}),
            last_consolidated=int(data.get("last_consolidated") or 0),
        )


class SessionManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.session_dir = workspace / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Session] = {}
        self._delete_callbacks: List[Callable[[str], None]] = []
        self._index_lock = threading.Lock()
        self._index = sqlite3.connect(
            str(self.session_dir / "message_index.sqlite3"),
            check_same_thread=False,
        )
        self._fts_enabled = self._initialize_index()
        self._closed = False
        self._rebuild_index()

    def get_or_create(self, key: str) -> Session:
        if key in self._cache:
            return self._cache[key]
        path = self._path(key)
        if not path.exists():
            legacy_path = self._legacy_path(key)
            if legacy_path.exists():
                try:
                    legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    legacy_data = {}
                if str(legacy_data.get("key") or "") == key:
                    path = legacy_path
        if path.exists():
            try:
                session = Session.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError("Session file is unreadable: %s" % path) from exc
        else:
            session = Session(key=key)
        self._cache[key] = session
        return session

    def peek_next_message_id(self, session_key: str) -> str:
        session = self.get_or_create(session_key)
        return "%s:%d" % (session_key, len(session.messages))

    def save(self, session: Session) -> None:
        path = self._path(session.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            ".%s.%d.%s.tmp" % (path.name, os.getpid(), uuid4().hex)
        )
        payload = json.dumps(session.to_json(), ensure_ascii=False, indent=2)
        try:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, path)
            try:
                self._index_session(session)
            except sqlite3.Error:
                logger.exception("failed to update message search index")
                self._fts_enabled = False
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    async def save_async(self, session: Session) -> None:
        """Persist session without blocking the channel event loop."""

        await asyncio.to_thread(self.save, session)

    def search_messages(self, query: str, limit: int = 10) -> List[JsonDict]:
        needle = query.lower().strip()
        if not needle:
            return []
        indexed = self._search_index(query, limit)
        if indexed is not None:
            return indexed
        results: List[JsonDict] = []
        for session in self._all_sessions():
            for index, msg in enumerate(session.messages):
                content = str(msg.get("content") or "")
                if needle in content.lower():
                    results.append(
                        {
                            "source_ref": "%s:%d" % (session.key, index),
                            "session_key": session.key,
                            "role": msg.get("role"),
                            "content": content[:500],
                            "timestamp": msg.get("timestamp", ""),
                        }
                    )
                    if len(results) >= limit:
                        return results
        return results

    def fetch_messages(self, source_ref: str, context: int = 2) -> List[JsonDict]:
        if ":" not in source_ref:
            return []
        session_key, raw_index = source_ref.rsplit(":", 1)
        try:
            index = int(raw_index)
        except ValueError:
            return []
        session = self.get_or_create(session_key)
        start = max(0, index - max(0, context))
        end = min(len(session.messages), index + max(0, context) + 1)
        return [
            {
                "source_ref": "%s:%d" % (session.key, i),
                "role": session.messages[i].get("role"),
                "content": session.messages[i].get("content", ""),
                "timestamp": session.messages[i].get("timestamp", ""),
            }
            for i in range(start, end)
        ]

    def list_sessions(self) -> List[JsonDict]:
        return [
            {
                "key": session.key,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "message_count": len(session.messages),
                "metadata": dict(session.metadata),
            }
            for session in sorted(
                self._all_sessions(), key=lambda item: item.updated_at, reverse=True
            )
        ]

    def get_channel_metadata(self, channel: str) -> List[JsonDict]:
        """Expose the narrow metadata projection used by Reference channels."""

        prefix = f"{channel}:"
        return [
            {
                "chat_id": str(item["key"])[len(prefix):],
                "metadata": dict(item.get("metadata") or {}),
            }
            for item in self.list_sessions()
            if str(item.get("key") or "").startswith(prefix)
        ]

    def delete_session(self, key: str) -> bool:
        existed = key in self._cache
        self._cache.pop(key, None)
        for path in (self._path(key), self._legacy_path(key)):
            if path.exists():
                path.unlink()
                existed = True
        if self._fts_enabled:
            with self._index_lock:
                self._index.execute(
                    "DELETE FROM message_fts WHERE session_key = ?", (key,)
                )
                self._index.commit()
        if existed:
            for callback in list(self._delete_callbacks):
                try:
                    callback(key)
                except Exception:
                    logger.exception("session delete callback failed for %s", key)
        return existed

    def on_delete(self, callback: Callable[[str], None]) -> None:
        if callback not in self._delete_callbacks:
            self._delete_callbacks.append(callback)

    def _all_sessions(self) -> List[Session]:
        sessions = list(self._cache.values())
        cached = {s.key for s in sessions}
        for path in sorted(self.session_dir.glob("*.json")):
            # 损坏的 session 文件必须暴露：静默跳过会让管理接口少列一条会话，
            # 用户看到的是“记录不见了”，而不是“这个文件坏了”。
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("Session file is unreadable: %s" % path) from exc
            key = str(data.get("key") or "")
            if not key:
                raise RuntimeError("Session file has no key: %s" % path)
            if key not in cached:
                sessions.append(Session.from_json(data))
        return sessions

    def _path(self, key: str) -> Path:
        return self.session_dir / ("%s.json" % _safe_key(key))

    def _legacy_path(self, key: str) -> Path:
        return self.session_dir / ("%s.json" % _legacy_safe_key(key))

    def _initialize_index(self) -> bool:
        with self._index_lock:
            self._index.execute("PRAGMA journal_mode=WAL")
            self._index.execute("PRAGMA synchronous=NORMAL")
            try:
                self._index.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
                    "source_ref UNINDEXED, session_key UNINDEXED, role UNINDEXED, "
                    "content, timestamp UNINDEXED, tokenize='trigram')"
                )
                self._index.commit()
                return True
            except sqlite3.OperationalError:
                self._index.rollback()
                try:
                    self._index.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
                        "source_ref UNINDEXED, session_key UNINDEXED, role UNINDEXED, "
                        "content, timestamp UNINDEXED)"
                    )
                    self._index.commit()
                    return True
                except sqlite3.OperationalError:
                    self._index.rollback()
                    return False

    def _rebuild_index(self) -> None:
        if not self._fts_enabled:
            return
        sessions = []
        for path in sorted(self.session_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("key"):
                    sessions.append(Session.from_json(payload))
            except (OSError, ValueError, TypeError):
                continue
        with self._index_lock:
            self._index.execute("DELETE FROM message_fts")
            for session in sessions:
                self._insert_index_rows(session)
            self._index.commit()

    def _index_session(self, session: Session) -> None:
        if not self._fts_enabled:
            return
        with self._index_lock:
            self._index.execute(
                "DELETE FROM message_fts WHERE session_key = ?", (session.key,)
            )
            self._insert_index_rows(session)
            self._index.commit()

    def _insert_index_rows(self, session: Session) -> None:
        self._index.executemany(
            "INSERT INTO message_fts(source_ref, session_key, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "%s:%d" % (session.key, index),
                    session.key,
                    str(message.get("role") or ""),
                    str(message.get("content") or ""),
                    str(message.get("timestamp") or ""),
                )
                for index, message in enumerate(session.messages)
                if str(message.get("content") or "")
            ],
        )

    def _search_index(self, query: str, limit: int) -> List[JsonDict] | None:
        if not self._fts_enabled:
            return None
        if len(query.strip()) < 3:
            return None
        phrase = '"%s"' % query.replace('"', '""')
        try:
            with self._index_lock:
                rows = self._index.execute(
                    "SELECT source_ref, session_key, role, content, timestamp "
                    "FROM message_fts WHERE message_fts MATCH ? "
                    "ORDER BY bm25(message_fts), timestamp DESC LIMIT ?",
                    (phrase, max(1, int(limit))),
                ).fetchall()
        except sqlite3.OperationalError:
            return None
        return [
            {
                "source_ref": row[0],
                "session_key": row[1],
                "role": row[2],
                "content": str(row[3])[:500],
                "timestamp": row[4],
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._index_lock:
            if self._closed:
                return
            self._index.close()
            self._closed = True
