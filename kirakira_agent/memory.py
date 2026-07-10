"""Markdown-backed memory runtime and searchable memory tools."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from kirakira_agent.session import Session, SessionManager


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _tokenize(text: str) -> set[str]:
    lowered = text.lower()
    ascii_words = set(re.findall(r"[a-z0-9_\-]{2,}", lowered))
    cjk = set(re.findall(r"[\u4e00-\u9fff]{2,}", lowered))
    return ascii_words | cjk


@dataclass
class MemoryRecord:
    id: str
    content: str
    created_at: str = field(default_factory=_now)
    source_ref: str = ""
    status: str = "active"

    def to_json(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at,
            "source_ref": self.source_ref,
            "status": self.status,
        }


class MarkdownMemoryStore:
    def __init__(self, workspace: Path) -> None:
        self.root = workspace / "memory"
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory_path = self.root / "MEMORY.md"
        self.self_path = self.root / "SELF.md"
        self.recent_path = self.root / "RECENT_CONTEXT.md"
        for path, title in (
            (self.memory_path, "# Long-Term Memory\n"),
            (self.self_path, "# Self Model\n"),
            (self.recent_path, "# Recent Context\n"),
        ):
            if not path.exists():
                path.write_text(title, encoding="utf-8")

    def read_long_term(self) -> str:
        return self.memory_path.read_text(encoding="utf-8")

    def read_self(self) -> str:
        return self.self_path.read_text(encoding="utf-8")

    def read_recent_context(self) -> str:
        return self.recent_path.read_text(encoding="utf-8")

    def append_recent(self, line: str) -> None:
        text = self.read_recent_context().rstrip()
        updated = text + "\n- %s\n" % line.strip()
        lines = updated.splitlines()
        if len(lines) > 80:
            lines = [lines[0]] + lines[-79:]
        self.recent_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def append_memory(self, record: MemoryRecord) -> None:
        with self.memory_path.open("a", encoding="utf-8") as fh:
            source = " source=%s" % record.source_ref if record.source_ref else ""
            fh.write("- [%s]%s %s\n" % (record.id, source, record.content.strip()))


class MemoryRuntime:
    def __init__(self, workspace: Path, session_manager: SessionManager | None = None) -> None:
        self.workspace = workspace
        self.store = MarkdownMemoryStore(workspace)
        self.session_manager = session_manager
        self.items_path = self.store.root / "items.json"
        self._records: List[MemoryRecord] = []
        self._load()

    def memorize(self, content: str, source_ref: str = "") -> MemoryRecord:
        content = content.strip()
        if not content:
            raise ValueError("memory content is empty")
        record = MemoryRecord(
            id="mem_%04d" % (len(self._records) + 1),
            content=content,
            source_ref=source_ref,
        )
        self._records.append(record)
        self.store.append_memory(record)
        self._save()
        return record

    def recall(self, query: str, limit: int = 5) -> List[MemoryRecord]:
        q_tokens = _tokenize(query)
        scored: List[tuple[int, MemoryRecord]] = []
        for record in self._records:
            if record.status != "active":
                continue
            tokens = _tokenize(record.content)
            score = len(q_tokens & tokens)
            if query.lower().strip() and query.lower().strip() in record.content.lower():
                score += 5
            if score > 0 or not q_tokens:
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [record for _, record in scored[: max(1, limit)]]

    def forget(self, ids: List[str]) -> List[str]:
        forgotten: List[str] = []
        wanted = set(ids)
        for record in self._records:
            if record.id in wanted and record.status == "active":
                record.status = "forgotten"
                forgotten.append(record.id)
        if forgotten:
            self._save()
        return forgotten

    def build_retrieval_block(self, query: str, limit: int = 5) -> str:
        records = self.recall(query, limit=limit)
        if not records:
            return ""
        lines = ["## Retrieved Long-Term Memory"]
        for record in records:
            source = " source_ref=%s" % record.source_ref if record.source_ref else ""
            lines.append("- [%s]%s %s" % (record.id, source, record.content))
        return "\n".join(lines)

    def consolidate_turn(self, session: Session, user_content: str, assistant_reply: str) -> None:
        summary = "user: %s | assistant: %s" % (
            user_content.strip().replace("\n", " ")[:220],
            assistant_reply.strip().replace("\n", " ")[:220],
        )
        self.store.append_recent(summary)
        maybe_memory = self._extract_explicit_memory(user_content)
        if maybe_memory:
            source_ref = "%s:%d" % (session.key, max(0, len(session.messages) - 2))
            self.memorize(maybe_memory, source_ref=source_ref)
        session.last_consolidated = len(session.messages)

    def search_messages(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        if self.session_manager is None:
            return []
        return self.session_manager.search_messages(query, limit=limit)  # type: ignore[return-value]

    def fetch_messages(self, source_ref: str, context: int = 2) -> List[Dict[str, str]]:
        if self.session_manager is None:
            return []
        return self.session_manager.fetch_messages(source_ref, context=context)  # type: ignore[return-value]

    def _extract_explicit_memory(self, text: str) -> str:
        patterns = [
            r"(?:请)?记住[:：]\s*(.+)",
            r"以后(?:你)?要记得[:：]?\s*(.+)",
            r"下次(?:你)?要记得[:：]?\s*(.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.S)
            if match:
                return match.group(1).strip()
        return ""

    def _load(self) -> None:
        if not self.items_path.exists():
            self._records = []
            return
        data = json.loads(self.items_path.read_text(encoding="utf-8"))
        self._records = [
            MemoryRecord(
                id=str(item.get("id") or ""),
                content=str(item.get("content") or ""),
                created_at=str(item.get("created_at") or _now()),
                source_ref=str(item.get("source_ref") or ""),
                status=str(item.get("status") or "active"),
            )
            for item in data
            if item.get("id") and item.get("content")
        ]

    def _save(self) -> None:
        self.items_path.write_text(
            json.dumps([r.to_json() for r in self._records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

