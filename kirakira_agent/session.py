"""Persistent chat sessions with tool-chain aware history reconstruction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

JsonDict = Dict[str, Any]


def _safe_key(key: str) -> str:
    return re.sub(r"[^\w.-]", "_", key)


def _truncate_tool_result(value: object, limit: int = 10000) -> str:
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    keep = max(0, limit - 40)
    return text[:keep] + "\n... (%d characters truncated)" % (len(text) - keep)


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

    def get_history(self, max_messages: int = 80) -> List[JsonDict]:
        selected = self.messages[-max_messages:] if max_messages > 0 else []
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
                out.append(
                    {
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
                )
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
                if msg.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = msg["reasoning_content"]
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

    def get_or_create(self, key: str) -> Session:
        if key in self._cache:
            return self._cache[key]
        path = self._path(key)
        if path.exists():
            session = Session.from_json(json.loads(path.read_text(encoding="utf-8")))
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
        path.write_text(
            json.dumps(session.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def search_messages(self, query: str, limit: int = 10) -> List[JsonDict]:
        needle = query.lower().strip()
        if not needle:
            return []
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

    def _all_sessions(self) -> List[Session]:
        sessions = list(self._cache.values())
        cached = {s.key for s in sessions}
        for path in sorted(self.session_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                key = str(data.get("key") or "")
                if key and key not in cached:
                    sessions.append(Session.from_json(data))
            except Exception:
                continue
        return sessions

    def _path(self, key: str) -> Path:
        return self.session_dir / ("%s.json" % _safe_key(key))

