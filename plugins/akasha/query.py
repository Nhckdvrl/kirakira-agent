"""Session-scoped, replayable memory reads for applications and evaluations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager

from plugins.akasha.contract import (
    MemoryQuery,
    MemoryReadout,
    MemorySourceQuery,
    MemorySourceReadout,
)
from plugins.ledger.contract import ContentPart, Input, MessageCatalog, Output

from .contract import ProgramSource
from .recall_tool import RecallTool
from .recalls import RecallRecords


class MemoryQueries:
    """Reuse the tool's source validation and receipts without a model tool call."""

    def __init__(
        self,
        *,
        capture: Callable[[Mapping[str, object]], Mapping[str, object]],
        open_recall: Callable[[Mapping[str, object]], AbstractAsyncContextManager[RecallTool]],
        catalog: MessageCatalog,
        records: Callable[[], RecallRecords],
        check_scope: Callable[[str], object],
    ):
        self._capture = capture
        self._open = open_recall
        self._catalog = catalog
        self._records = records
        self._check_scope = check_scope
        self._lock = asyncio.Lock()

    async def source(self, request: MemorySourceQuery) -> MemorySourceReadout:
        """Expand a previously presented source without embedding or graph reads."""
        if request.session_id not in self._catalog.snapshot_attributes():
            raise ValueError("source read requires an existing session")
        route = self._check_scope(request.session_id)
        parent = self._records().read(request.retrieval_ref)
        if parent is None:
            raise ValueError("unknown memory retrieval reference")
        origin = parent.source.session_id
        if not origin or self._check_scope(origin) != route:
            raise ValueError("source receipt belongs to another memory scope")
        if request.message_id not in parent.presented_message_ids:
            raise ValueError("message was not presented by this retrieval")
        hit = next(hit for hit in parent.hits if request.message_id in hit.message_ids)
        message = self._catalog.reader(hit.session_id).get(request.message_id)
        if message is None or not isinstance(message.body, (Input, Output)):
            raise ValueError("memory source is unavailable")
        text = "".join(part.value for part in message.body.parts
                       if isinstance(part, ContentPart) and part.kind == "text" and isinstance(part.value, str))
        if request.offset >= len(text):
            raise ValueError("source offset is at or beyond the end of the message")
        end = min(len(text), request.offset + request.length)
        return MemorySourceReadout.model_validate_json(json.dumps({
            "retrieval_ref": request.retrieval_ref, "text": text[request.offset:end],
            "evidence": {"message_id": message.message_id, "session_id": message.session_id,
                         "seq": message.seq, "recorded_at": message.recorded_at.isoformat(),
                         "author": message.author, "excerpt": {"message_id": message.message_id,
                             "start": request.offset, "end": end, "source_chars": len(text)}},
            "next_offset": end if end < len(text) else None,
        }))

    async def recall(self, request: MemoryQuery) -> MemoryReadout:
        async with self._lock:
            if request.session_id not in self._catalog.snapshot_attributes():
                raise ValueError("memory query requires an existing session")
            # Policy denial also applies to receipt replay.
            self._check_scope(request.session_id)
            digest = hashlib.sha256(json.dumps(
                [request.session_id, request.key], ensure_ascii=False,
            ).encode()).hexdigest()
            key = "program:" + digest
            identity = "tool:" + key
            saved = self._records().read(identity)
            if saved is not None and (
                not isinstance(saved.source, ProgramSource)
                or saved.source.session_id != request.session_id
                or saved.source.query != request.query
                or saved.limit != request.limit
                or saved.max_chars != request.max_chars
                or saved.time_start != request.time_start
                or saved.time_end != request.time_end
                or saved.selection != request.selection
                or saved.presentation != request.presentation
            ):
                raise ValueError("memory query key already belongs to a different request")
            async with self._open(self._capture({})) as tool:
                if saved is None:
                    prepared = await tool.prepare(request.model_dump(
                        mode="json", exclude={"key", "session_id", "max_chars"},
                    ), session_id=request.session_id)
                    if isinstance(prepared, str):
                        raise ValueError(prepared)
                    result = await tool.invoke(key, {**prepared, "max_chars": request.max_chars})
                else:
                    result = await tool.query(key)
            if result is None or result.outcome != "success":
                raise RuntimeError("memory read did not produce a successful receipt")
            record = self._records().read(identity)
            if record is None:
                raise RuntimeError("memory read receipt is missing")
            sessions = {message_id: hit.session_id
                        for hit in record.hits for message_id in hit.message_ids}
            evidence = []
            spans = {span.message_id: span for span in record.presented_spans}
            for message_id in record.presented_message_ids:
                message = self._catalog.reader(sessions[message_id]).get(message_id)
                if message is None:
                    raise ValueError("memory evidence source is missing")
                span = spans.get(message_id)
                evidence.append({"message_id": message_id, "session_id": message.session_id,
                    "seq": message.seq, "recorded_at": message.recorded_at.isoformat(), "author": message.author,
                    "excerpt": None if span is None else span.model_dump(mode="json")})
            # The first text part is transport metadata, not evidence material.
            material = "\n\n".join(str(part.value) for part in result.parts[1:]
                                   if isinstance(part, ContentPart) and part.kind == "text")
            if len(material) > request.max_chars:
                raise RuntimeError("memory material exceeded its declared budget")
            # Generation-local receipts stay private. The service publishes the
            # process-wide contract, just like its serialized Gateway boundary.
            return MemoryReadout.model_validate_json(json.dumps({"retrieval_ref": identity,
                "record": record.model_dump(mode="json"), "material": material, "evidence": evidence}))
