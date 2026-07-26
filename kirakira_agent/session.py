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
from typing import Any, Callable, Dict, List, Optional

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
        # 派生索引库放在 workspace 根的 sessions.db,与 Reference 的命名一致——
        # akasha 引擎把它当真相源读(见 messages 投影表)。canonical 仍是 per-session
        # JSON;这个库每次启动都由 _rebuild_index 从 JSON 重建,所以换路径是自愈的。
        self._index = sqlite3.connect(
            str(workspace / "sessions.db"),
            check_same_thread=False,
        )
        self._fts_enabled = self._initialize_index()
        self._closed = False
        self._rebuild_index()
        self._drop_legacy_index()

    def _drop_legacy_index(self) -> None:
        """删掉换路径前的旧索引库(sessions/message_index.sqlite3)。

        它是纯派生物,新库已在 __init__ 里从 JSON 全量重建过,留着只会让人误以为
        还有第二份真相。删不掉不影响运行,忽略即可。
        """
        for suffix in ("", "-wal", "-shm"):
            legacy = self.session_dir / ("message_index.sqlite3%s" % suffix)
            try:
                legacy.unlink(missing_ok=True)
            except OSError:
                pass

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

    def session_exists(self, key: str) -> bool:
        """判断会话是否已经存在,不像 ``get_or_create`` 那样顺带创建。

        控制面用它区分"resume 一个已有 thread"和"thread 不存在"。
        """
        if key in self._cache:
            return True
        return self._path(key).exists() or self._legacy_path(key).exists()

    def get_session_meta(self, key: str) -> Optional[JsonDict]:
        """读取会话元数据;不存在时返回 None(= Reference SessionStore 同名方法)。"""
        if not self.session_exists(key):
            return None
        session = self.get_or_create(key)
        return {
            "key": session.key,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "metadata": dict(session.metadata),
        }

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
                self._index.execute(
                    "DELETE FROM messages WHERE session_key = ?", (key,)
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
            # messages 投影表:akasha 引擎的真相源是"会话消息的关系表"
            # (它的 DESCRIPTOR.notes 写着 truth=sessions.db/messages),而 kirakira 的
            # canonical 存储是 per-session JSON。这里把消息投影成 Reference 同形的表,
            # 与 FTS 共用同一个派生索引库和同一个维护点——JSON 仍是唯一权威,
            # 这张表和 FTS 一样是可重建的派生物(_rebuild_index 会一起重建)。
            self._index.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id TEXT PRIMARY KEY, session_key TEXT NOT NULL, seq INTEGER NOT NULL, "
                "role TEXT NOT NULL, content TEXT NOT NULL, ts TEXT NOT NULL)"
            )
            self._index.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session_seq "
                "ON messages(session_key, seq)"
            )
            self._initialize_messages_fts()
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

    def _reset_messages_projection(self) -> None:
        """把 messages 投影与它的外部内容 FTS 一起清空重建。

        不能只 `DELETE FROM messages`:那会触发 `messages_ad` 去删 FTS 索引项,
        而外部内容 FTS 一旦与内容表不同步(例如投影表先于 FTS 存在的老库),
        SQLite 会直接报 `database disk image is malformed`。投影是纯派生物,
        所以做法是**先拆掉触发器与索引,清表,再原样建回来**——无论之前多不一致都能收敛。
        """
        for statement in (
            "DROP TRIGGER IF EXISTS messages_ai",
            "DROP TRIGGER IF EXISTS messages_ad",
            "DROP TRIGGER IF EXISTS messages_au",
            "DROP TABLE IF EXISTS messages_fts",
            "DELETE FROM messages",
        ):
            try:
                self._index.execute(statement)
            except sqlite3.DatabaseError:
                # 连 DELETE 都失败说明表本身坏了,重建它比抢救更省事。
                self._index.rollback()
                self._index.execute("DROP TABLE IF EXISTS messages")
                self._index.execute(
                    "CREATE TABLE messages ("
                    "id TEXT PRIMARY KEY, session_key TEXT NOT NULL, seq INTEGER NOT NULL, "
                    "role TEXT NOT NULL, content TEXT NOT NULL, ts TEXT NOT NULL)"
                )
                self._index.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_session_seq "
                    "ON messages(session_key, seq)"
                )
                break
        self._initialize_messages_fts()

    def _initialize_messages_fts(self) -> None:
        """messages 的外部内容 FTS(照 Reference `session/store.py`)。

        akasha 的关键词 lane 直接查 `messages_fts`,所以表名、`content='messages'`
        外部内容模式与三个同步触发器都要一致。触发器让它随投影表增量维护,
        不必每次全量重扫。trigram 不可用时退回默认分词。
        """
        for tokenize in ("tokenize='trigram'", ""):
            try:
                self._index.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                    "content, content='messages', content_rowid='rowid'%s)"
                    % ((", " + tokenize) if tokenize else "")
                )
                break
            except sqlite3.OperationalError:
                self._index.rollback()
        else:
            return
        for statement in (
            "CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN "
            "INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content); END",
            "CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN "
            "INSERT INTO messages_fts(messages_fts, rowid, content) "
            "VALUES('delete', old.rowid, old.content); END",
            "CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN "
            "INSERT INTO messages_fts(messages_fts, rowid, content) "
            "VALUES('delete', old.rowid, old.content); "
            "INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content); END",
        ):
            self._index.execute(statement)
        self._index.commit()

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
            self._reset_messages_projection()
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
            self._index.execute(
                "DELETE FROM messages WHERE session_key = ?", (session.key,)
            )
            self._insert_index_rows(session)
            self._index.commit()

    def _insert_index_rows(self, session: Session) -> None:
        rows = [
            (
                "%s:%d" % (session.key, index),
                session.key,
                index,
                str(message.get("role") or ""),
                str(message.get("content") or ""),
                str(message.get("timestamp") or ""),
            )
            for index, message in enumerate(session.messages)
            if str(message.get("content") or "")
        ]
        self._index.executemany(
            "INSERT INTO message_fts(source_ref, session_key, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            [(row[0], row[1], row[3], row[4], row[5]) for row in rows],
        )
        # messages 投影与 FTS 同一事务写入,两者不会出现只更新一边的中间态。
        self._index.executemany(
            "INSERT OR REPLACE INTO messages(id, session_key, seq, role, content, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
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
