"""Markdown-backed memory runtime and searchable memory tools."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from kirakira_agent.retrieval import (
    LEXICAL_RRF_WEIGHT,
    RetrievalRequest,
    RetrievalResult,
    RetrievalTrace,
    hotness_boost,
    plan_injection,
    rrf_fuse,
)
from kirakira_agent.session import Session, SessionManager
from kirakira_agent.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _tokenize(text: str) -> set[str]:
    lowered = text.lower()
    ascii_words = set(re.findall(r"[a-z0-9_\-]{2,}", lowered))
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", lowered)
    cjk = {
        run[index : index + 2]
        for run in cjk_runs
        for index in range(max(1, len(run) - 1))
        if run[index : index + 2]
    }
    return ascii_words | cjk


def _normalize_content(text: str) -> str:
    return " ".join(text.lower().split())


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".%s.%d.%s.tmp" % (path.name, os.getpid(), uuid4().hex))
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


@dataclass
class MemoryRecord:
    id: str
    content: str
    created_at: str = field(default_factory=_now)
    source_ref: str = ""
    status: str = "active"
    memory_type: str = "requested_memory"
    reinforcement: int = 1
    updated_at: str = field(default_factory=_now)
    embedding: List[float] | None = None

    def to_json(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at,
            "source_ref": self.source_ref,
            "status": self.status,
            "memory_type": self.memory_type,
            "reinforcement": self.reinforcement,
            "updated_at": self.updated_at,
            "embedding": self.embedding,
        }

    def to_public_json(self) -> Dict[str, object]:
        payload = self.to_json()
        payload.pop("embedding", None)
        return payload


class MarkdownMemoryStore:
    def __init__(self, workspace: Path) -> None:
        self.root = workspace / "memory"
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory_path = self.root / "MEMORY.md"
        self.self_path = self.root / "SELF.md"
        self.recent_path = self.root / "RECENT_CONTEXT.md"
        self.history_path = self.root / "HISTORY.md"
        self.pending_path = self.root / "PENDING.md"
        for path, title in (
            (self.memory_path, "# Long-Term Memory\n"),
            (self.self_path, "# Self Model\n"),
            (self.recent_path, "# Recent Context\n"),
            (self.history_path, "# History\n"),
            (self.pending_path, "# Pending Memory\n"),
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
        _atomic_write(self.recent_path, "\n".join(lines) + "\n")

    def append_memory(self, record: MemoryRecord) -> None:
        self.sync_memory_records([record])

    def sync_memory_records(self, records: List[MemoryRecord]) -> None:
        start = "<!-- kirakira:managed-memory:start -->"
        end = "<!-- kirakira:managed-memory:end -->"
        existing = self.read_long_term().rstrip()
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
        existing = pattern.sub("", existing).rstrip()
        # Migrate lines emitted by the early runtime before managed blocks existed.
        existing = re.sub(r"(?m)^- \[mem_\d+\](?: source=\S+)? .*\n?", "", existing).rstrip()
        lines = [start]
        for record in records:
            if record.status != "active":
                continue
            source = " source=%s" % record.source_ref if record.source_ref else ""
            lines.append(
                "- [%s type=%s reinforced=%d]%s %s"
                % (
                    record.id,
                    record.memory_type,
                    record.reinforcement,
                    source,
                    record.content.strip(),
                )
            )
        lines.append(end)
        _atomic_write(self.memory_path, existing + "\n\n" + "\n".join(lines) + "\n")

    def append_history(self, source_ref: str, summary: str) -> None:
        marker = "<!-- turn:%s -->" % source_ref
        existing = self.history_path.read_text(encoding="utf-8")
        if marker in existing:
            return
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write("%s\n[%s] %s\n" % (marker, timestamp, summary.strip()))


class MemoryRuntime:
    def __init__(self, workspace: Path, session_manager: SessionManager | None = None) -> None:
        self.workspace = workspace
        self.store = MarkdownMemoryStore(workspace)
        self.session_manager = session_manager
        self.items_path = self.store.root / "items.json"
        self._records: List[MemoryRecord] = []
        self._record_lock = threading.RLock()
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self.embedding_client: EmbeddingClient | None = None
        self._load()
        if self.session_manager is not None:
            self.session_manager.on_delete(self._forget_session_memories)

    def configure_embeddings(
        self, *, base_url: str, api_key: str, model: str
    ) -> None:
        if base_url.strip() and model.strip():
            self.embedding_client = EmbeddingClient(
                base_url=base_url,
                api_key=api_key,
                model=model,
            )

    def memorize(
        self,
        content: str,
        source_ref: str = "",
        memory_type: str = "requested_memory",
    ) -> MemoryRecord:
        with self._record_lock:
            content = content.strip()
            if not content:
                raise ValueError("memory content is empty")
            normalized = _normalize_content(content)
            for record in self._records:
                if record.status == "active" and _normalize_content(record.content) == normalized:
                    if source_ref and source_ref == record.source_ref:
                        return record
                    record.reinforcement += 1
                    record.updated_at = _now()
                    if source_ref:
                        record.source_ref = source_ref
                    self._save()
                    return record
            record = MemoryRecord(
                id=self._next_id(),
                content=content,
                source_ref=source_ref,
                memory_type=memory_type.strip() or "requested_memory",
                embedding=self._embed_for_store(content),
            )
            self._records.append(record)
            self._save()
            return record

    def candidates(
        self,
        memory_types: List[str] | None = None,
        since: str = "",
        until: str = "",
    ) -> List[MemoryRecord]:
        """按 type/时间过滤出候选集合；排序交给各 lane。"""

        allowed_types = {item for item in (memory_types or []) if item}
        since_dt = self._parse_optional_time(since)
        until_dt = self._parse_optional_time(until)
        with self._record_lock:
            records = list(self._records)
        selected: List[MemoryRecord] = []
        for record in records:
            if record.status != "active":
                continue
            if allowed_types and record.memory_type not in allowed_types:
                continue
            created = self._parse_optional_time(record.created_at)
            if since_dt and created and created < since_dt:
                continue
            if until_dt and created and created > until_dt:
                continue
            selected.append(record)
        return selected

    def lexical_lane(
        self, query: str, records: List[MemoryRecord]
    ) -> List[MemoryRecord]:
        """词法 lane：擅长变量名、命令、路径、错误码这类精确实体。"""

        q_tokens = _tokenize(query)
        needle = query.lower().strip()
        scored: List[tuple[float, str, MemoryRecord]] = []
        for record in records:
            tokens = _tokenize(record.content)
            overlap = len(q_tokens & tokens)
            score = overlap / max(1.0, math.sqrt(len(q_tokens) * max(1, len(tokens))))
            # 整串命中是很强的信号，但只在本 lane 内部抬名次，不会跨 lane 污染分数。
            if needle and needle in record.content.lower():
                score += 1.0
            if score <= 0:
                continue
            scored.append((score, record.created_at, record))
        scored.sort(key=lambda item: (-item[0], item[1]), reverse=False)
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, _, record in scored]

    def vector_lane(
        self, query: str, records: List[MemoryRecord], *, threshold: float = 0.25
    ) -> List[MemoryRecord]:
        """语义 lane：擅长口语化表达和同义改写。向量不可用时返回空，退化为纯词法。"""

        query_embedding = self._embed_for_query(query) if query.strip() else None
        if query_embedding is None:
            return []
        scored: List[tuple[float, str, MemoryRecord]] = []
        for record in records:
            semantic = self._cosine(query_embedding, record.embedding)
            if semantic is None or semantic < threshold:
                continue
            scored.append((semantic, record.created_at, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, _, record in scored]

    def recall(
        self,
        query: str,
        limit: int = 5,
        memory_types: List[str] | None = None,
        since: str = "",
        until: str = "",
    ) -> List[MemoryRecord]:
        """多路召回 + RRF 融合。

        不再用 `semantic * 0.75 + lexical * 0.25`：那是把尺度不可比的两个原始分数直接
        相加。RRF 只看各 lane 内部的名次，因此 lane 之间不需要可比。
        """

        return self._recall_with_trace(query, limit, memory_types, since, until)[0]

    def _recall_with_trace(
        self,
        query: str,
        limit: int,
        memory_types: List[str] | None,
        since: str,
        until: str,
    ) -> tuple[List[MemoryRecord], RetrievalTrace]:
        """召回并同时产出 trace。lane 只跑一次——vector lane 会打 embedding 接口，
        为了记 trace 再跑一遍等于每轮多花一次网络往返。"""

        trace = RetrievalTrace(used_vector=self.embedding_client is not None)
        records = self.candidates(memory_types, since, until)
        if not records:
            return [], trace
        if not query.strip():
            # 无 query 时没有"相关性"可言，按时间倒序给最近的。
            selected = sorted(records, key=lambda r: r.created_at, reverse=True)[
                : max(1, limit)
            ]
            trace.fused = len(selected)
            return selected, trace

        lexical = self.lexical_lane(query, records)
        vector = self.vector_lane(query, records)
        trace.lanes = {"lexical": len(lexical), "vector": len(vector)}
        fused = rrf_fuse(
            [("vector", 1.0, vector), ("lexical", LEXICAL_RRF_WEIGHT, lexical)]
        )
        by_id = {record.id: record for record in records}
        now = datetime.now().astimezone()
        boosted: List[tuple[float, str, MemoryRecord]] = []
        for record_id, score in fused:
            record = by_id.get(record_id)
            if record is None:
                continue
            # 强化次数的加成随时间半衰，陈年旧记忆不会永远压住新记忆。
            score *= hotness_boost(
                record.reinforcement,
                self._parse_optional_time(record.updated_at),
                now,
            )
            boosted.append((score, record.created_at, record))
        boosted.sort(key=lambda item: (-item[0], item[1]))
        selected = [record for _, _, record in boosted[: max(1, limit)]]
        trace.fused = len(selected)
        return selected, trace

    def forget(self, ids: List[str]) -> List[str]:
        with self._record_lock:
            forgotten: List[str] = []
            wanted = set(ids)
            for record in self._records:
                if record.id in wanted and record.status == "active":
                    record.status = "forgotten"
                    forgotten.append(record.id)
            if forgotten:
                self._save()
            return forgotten

    def list_records(self, *, include_forgotten: bool = False) -> List[Dict[str, object]]:
        with self._record_lock:
            records = [
                record
                for record in self._records
                if include_forgotten or record.status == "active"
            ]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return [record.to_public_json() for record in records]

    def update_record(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        memory_type: str | None = None,
    ) -> bool:
        with self._record_lock:
            for record in self._records:
                if record.id != memory_id:
                    continue
                if content is not None:
                    value = content.strip()
                    if not value:
                        raise ValueError("memory content is empty")
                    record.content = value
                    record.embedding = self._embed_for_store(value)
                if memory_type is not None:
                    record.memory_type = memory_type.strip() or record.memory_type
                record.updated_at = _now()
                self._save()
                return True
            return False

    def build_retrieval_block(self, query: str, limit: int = 5) -> str:
        return self.retrieve(RetrievalRequest(query=query, limit=limit)).block

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """默认检索管线：多路召回 → RRF 融合 → 热度加权 → 注入预算。

        实现 `MemoryRetrievalPipeline` 协议，因此可以整体替换成别的策略。
        """

        records, trace = self._recall_with_trace(
            request.query,
            request.limit,
            list(request.memory_types) or None,
            request.since,
            request.until,
        )
        block, injected, truncated = plan_injection(records)
        trace.injected = injected
        trace.truncated = truncated
        return RetrievalResult(block=block, records=records, trace=trace)

    def consolidate_turn(self, session: Session, user_content: str, assistant_reply: str) -> None:
        summary = "user: %s | assistant: %s" % (
            user_content.strip().replace("\n", " ")[:220],
            assistant_reply.strip().replace("\n", " ")[:220],
        )
        self.store.append_recent(summary)
        source_ref = self._latest_user_source_ref(session)
        self.store.append_history(source_ref, summary)
        if self._last_assistant_used_memorize(session):
            return
        maybe_memory = self._extract_explicit_memory(user_content)
        if maybe_memory:
            self.memorize(maybe_memory, source_ref=source_ref)

    async def wait_for_session(self, session_key: str, timeout: float = 30.0) -> None:
        task = self._tasks.get(session_key)
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.1, timeout))
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def schedule_consolidation(
        self,
        session: Session,
        *,
        model_client: Any,
        model: str,
        min_messages: int = 6,
        keep_messages: int = 4,
    ) -> None:
        existing = self._tasks.get(session.key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._consolidate_session(
                session,
                model_client=model_client,
                model=model,
                min_messages=min_messages,
                keep_messages=keep_messages,
            ),
            name="memory-consolidation:%s" % session.key,
        )
        self._tasks[session.key] = task

        def done(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(session.key) is completed:
                self._tasks.pop(session.key, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("memory consolidation failed for %s", session.key)

        task.add_done_callback(done)

    async def shutdown(self, timeout: float = 30.0) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=max(0.1, timeout))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _consolidate_session(
        self,
        session: Session,
        *,
        model_client: Any,
        model: str,
        min_messages: int,
        keep_messages: int,
    ) -> None:
        lock = self._locks.setdefault(session.key, asyncio.Lock())
        async with lock:
            total = len(session.messages)
            start = max(0, min(int(session.last_consolidated), total))
            end = max(start, total - max(0, keep_messages))
            if end - start < max(2, min_messages):
                return
            selected = session.messages[start:end]
            first_user = next(
                (
                    index
                    for index, message in enumerate(selected)
                    if message.get("role") == "user"
                ),
                None,
            )
            if first_user is None:
                session.last_consolidated = end
                if self.session_manager is not None:
                    self.session_manager.save(session)
                return
            start += first_user
            selected = selected[first_user:]
            transcript = "\n".join(
                "[%s] %s" % (
                    str(message.get("role") or "unknown"),
                    str(message.get("content") or "")[:3000],
                )
                for message in selected
            )
            # 把已记事实喂给同一次调用：抽取和去重合并成一次判断，不额外打模型。
            # 词法阈值去重不安全（否定句相似度比真重复还高），只有语义判断做得了这件事。
            known = self._known_memory_digest(session.key, selected)
            prompt = (
                "从下面对话中提取可长期保留的信息。只把用户明确表达的稳定事实、偏好、"
                "身份、反复可用的操作规则写入 memories；不要把 assistant 的建议当用户事实。"
                "同时生成 1-3 条简短时间线摘要。仅返回 JSON："
                '{"memories":[{"content":"...","memory_type":"identity|preference|procedure|event"}],'
                '"history":["..."]}。没有可提取内容时数组留空。'
                + known
                + "\n\n对话：\n"
                + transcript
            )
            response = await asyncio.to_thread(
                model_client.complete,
                [{"role": "user", "content": prompt}],
                [],
                "",
                model,
                1200,
            )
            text = str(getattr(response, "text", "") or "").strip()
            payload = self._parse_consolidation_json(text)
            source_ref = "%s:%d-%d" % (session.key, start, end - 1)
            for item in payload.get("memories", [])[:10]:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "").strip()
                memory_type = str(item.get("memory_type") or "event").strip()
                if content:
                    await asyncio.to_thread(
                        self.memorize,
                        content,
                        source_ref,
                        memory_type,
                    )
            for index, summary in enumerate(payload.get("history", [])[:3]):
                value = str(summary or "").strip()
                if value:
                    self.store.append_history(
                        "%s:summary:%d" % (source_ref, index), value
                    )
            session.last_consolidated = end
            if self.session_manager is not None:
                self.session_manager.save(session)

    def _known_memory_digest(
        self,
        session_key: str,
        selected: List[JsonDict],
        limit: int = 30,
    ) -> str:
        """列出已记事实，让 consolidation 只抽新的。

        为什么要有这个：`memorize` 的去重是精确字符串匹配，而 consolidation 的 LLM 每次都会
        改写措辞，所以同一个事实会被存两遍（memorize 一条、consolidation 一条）。词法相似度
        阈值修不了——实测否定句"CI 不跑在 X"和"CI 跑在 X"的相似度(0.833)比真重复(0.727)还高，
        任何有效阈值都会把它们合并掉，让 agent 说反话。只有语义判断做得了，而 consolidation
        本来就要打一次 LLM，所以把去重并进这次调用，零额外往返。

        取两部分：本 session 已记的（`memorize` 刚写的，最可能被重复抽取），以及与本轮内容
        词法相关的旧记忆（跨 session 复述的情况）。
        """

        query = " ".join(
            str(message.get("content") or "")
            for message in selected
            if message.get("role") == "user"
        )[:2000]

        picked: List[MemoryRecord] = []
        seen: set[str] = set()
        with self._record_lock:
            same_session = [
                record
                for record in self._records
                if record.status == "active"
                and record.source_ref.startswith("%s:" % session_key)
            ]
        for record in reversed(same_session):
            if record.id not in seen:
                seen.add(record.id)
                picked.append(record)
            if len(picked) >= limit:
                break
        if query.strip() and len(picked) < limit:
            for record in self.recall(query, limit=limit - len(picked)):
                if record.id not in seen:
                    seen.add(record.id)
                    picked.append(record)
        if not picked:
            return ""

        lines = "\n".join(
            "- [%s] %s" % (record.memory_type, record.content[:160])
            for record in picked[:limit]
        )
        return (
            "\n\n以下事实**已经记录过**，不要再抽取：\n"
            + lines
            + "\n规则：\n"
            "- 只是换个说法表达上面某条事实 → 跳过，不要输出。\n"
            "- 上面某条事实需要修正或补充细节 → 输出完整的新版本，并沿用它原来的 memory_type。\n"
            "- 与上面某条**语义相反**（例如否定）→ 必须输出，这是修正，不是重复。\n"
            "- 只输出上面没有的新信息。"
        )

    @staticmethod
    def _parse_consolidation_json(text: str) -> Dict[str, Any]:
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            payload = json.loads(text)
        except ValueError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                return {"memories": [], "history": []}
            try:
                payload = json.loads(match.group(0))
            except ValueError:
                return {"memories": [], "history": []}
        if not isinstance(payload, dict):
            return {"memories": [], "history": []}
        memories = payload.get("memories")
        history = payload.get("history")
        return {
            "memories": memories if isinstance(memories, list) else [],
            "history": history if isinstance(history, list) else [],
        }

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
                memory_type=str(item.get("memory_type") or "requested_memory"),
                reinforcement=max(1, int(item.get("reinforcement") or 1)),
                updated_at=str(item.get("updated_at") or item.get("created_at") or _now()),
                embedding=[float(value) for value in item.get("embedding", [])]
                if isinstance(item.get("embedding"), list) and item.get("embedding")
                else None,
            )
            for item in data
            if item.get("id") and item.get("content")
        ]

    def _save(self) -> None:
        _atomic_write(
            self.items_path,
            json.dumps([r.to_json() for r in self._records], ensure_ascii=False, indent=2),
        )
        self.store.sync_memory_records(self._records)

    def _next_id(self) -> str:
        highest = 0
        for record in self._records:
            match = re.fullmatch(r"mem_(\d+)", record.id)
            if match:
                highest = max(highest, int(match.group(1)))
        return "mem_%04d" % (highest + 1)

    def _last_assistant_used_memorize(self, session: Session) -> bool:
        if not session.messages:
            return False
        message = session.messages[-1]
        if message.get("role") != "assistant":
            return False
        for group in message.get("tool_chain") or []:
            for call in group.get("calls") or []:
                if call.get("name") == "memorize":
                    return True
        return False

    def _embed_for_query(self, text: str) -> List[float] | None:
        """检索侧可以降级：拿不到向量就退回词法召回，本轮仍然有答案。"""

        if self.embedding_client is None or not text.strip():
            return None
        try:
            return self.embedding_client.embed(text)
        except Exception:
            logger.exception("embedding failed; falling back to lexical recall")
            return None

    def _embed_for_store(self, text: str) -> List[float] | None:
        """写入侧不能降级：配置了 embedding 却静默存入无向量记录，会让这条记忆此后
        永远无法被语义召回，且索引里一部分有向量一部分没有，是不可见的数据损坏。"""

        if self.embedding_client is None or not text.strip():
            return None
        try:
            return self.embedding_client.embed(text)
        except Exception as exc:
            raise RuntimeError(
                "embedding service failed while storing memory; refusing to write a "
                "record that could never be recalled semantically"
            ) from exc

    @staticmethod
    def _cosine(
        first: List[float] | None, second: List[float] | None
    ) -> float | None:
        if not first or not second or len(first) != len(second):
            return None
        dot = sum(a * b for a, b in zip(first, second))
        first_norm = math.sqrt(sum(value * value for value in first))
        second_norm = math.sqrt(sum(value * value for value in second))
        if first_norm <= 0 or second_norm <= 0:
            return None
        return dot / (first_norm * second_norm)

    @staticmethod
    def _parse_optional_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.astimezone()

    @staticmethod
    def _latest_user_source_ref(session: Session) -> str:
        for index in range(len(session.messages) - 1, -1, -1):
            if session.messages[index].get("role") == "user":
                return "%s:%d" % (session.key, index)
        return "%s:%d" % (session.key, len(session.messages))

    def _forget_session_memories(self, session_key: str) -> None:
        prefix = session_key + ":"
        with self._record_lock:
            changed = False
            for record in self._records:
                if record.status == "active" and record.source_ref.startswith(prefix):
                    record.status = "forgotten"
                    record.updated_at = _now()
                    changed = True
            if changed:
                self._save()
