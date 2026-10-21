"""Feed checkpoints own collection only; EventMail and Ledger own accepted facts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from lxml import etree

from core.common.file_io import run_file_io
from plugins.eventmail.contract import REQUESTED_REVIEW
from plugins.ledger.contract import (
    ContentPart,
    Input,
    Output,
    ToolCall,
    ToolResult,
    json_value,
)
from plugins.tools.contract import TOOLS

from . import webpages
from .contract import Feedback, SetEnabled, WatchSpec
from .feeds import MAX_BYTES, Entry, decode, feed_url, fingerprint

MAX_WATCHES = 100


def text(message) -> str:
    return "\n".join(
        str(part.value)
        for part in message.body.parts
        if isinstance(part, ContentPart) and part.kind == "text"
    )


class Service:
    def __init__(self, ctx, store, catalog, content, delivery, bindings, recalls):
        self.ctx, self.store, self.catalog = ctx, store, catalog
        self.content, self.delivery, self.bindings, self.recalls = (
            content,
            delivery,
            bindings,
            recalls,
        )
        self.http = httpx.AsyncClient(
            timeout=20, follow_redirects=True, max_redirects=5
        )
        self.changed = asyncio.Event()
        self.locks: dict[str, asyncio.Lock] = {}
        self.sources = {}
        self.source_lock = asyncio.Lock()

    def _watch(self, identity):
        record = self.store.read("watch:" + identity)
        if record is None:
            raise ValueError("跟进不存在")
        return dict(json_value(record.value))

    def _watches(self):
        return [
            dict(json_value(record.value))
            for _, record in self.store.scan(
                start="watch:", stop="watch;", limit=MAX_WATCHES
            )
        ]

    def _session(self, session_id):
        if (
            not self.catalog.exists(session_id)
            or self.catalog.attributes(session_id).visibility != "listed"
        ):
            raise ValueError("请先在当前对话发送消息")

    def _anchors(self, spec):
        self._session(spec.session_id)
        reader = self.catalog.reader(spec.session_id)
        result = []
        budget = min(1500, 3000 // len(spec.anchors))
        for identity in spec.anchors:
            message = reader.get(identity)
            if (
                message is None
                or not isinstance(message.body, Input)
                or message.author != "user"
                or message.source != "conversation"
                or not text(message)
            ):
                raise ValueError("跟进依据必须是当前对话中用户发送的原始消息")
            result.append(
                {
                    "message_id": identity,
                    "session_id": spec.session_id,
                    "recorded_at": message.recorded_at.isoformat(),
                    "text": text(message)[:budget],
                    "truncated": len(text(message)) > budget,
                }
            )
        return result

    async def _save(self, key, value):
        def commit(tx):
            current = tx.read(key)
            tx.save(
                key,
                value,
                expected_version=None if current is None else current.version,
            )

        await self.store.transact_async(commit)

    async def _source(self, identity):
        async with self.source_lock:
            if identity not in self.sources:
                source = self.content.bind("followups:" + identity)
                await self.ctx.effect(
                    lambda: source.close, label="followups.source:" + identity
                )
                self.sources[identity] = source
        return self.sources[identity]

    async def overview(self, session_id):
        def read():
            self._session(session_id)
            recent = [
                message
                for message in self.catalog.reader(session_id).snapshot()
                if isinstance(message.body, Input)
                and message.author == "user"
                and message.source == "conversation"
                and text(message)
            ][-12:]
            return {
                "project": self.catalog.attributes(session_id).dimension("project"),
                "anchors": [
                    {
                        "message_id": message.message_id,
                        "recorded_at": message.recorded_at.isoformat(),
                        "text": text(message)[:600],
                    }
                    for message in reversed(recent)
                ],
                "watches": [
                    watch
                    for watch in self._watches()
                    if watch["spec"]["session_id"] == session_id
                ],
            }

        return await run_file_io(read)

    async def create(self, spec):
        spec = WatchSpec.model_validate(spec.model_dump())
        spec = spec.model_copy(
            update={
                "url": feed_url(spec.url, github_releases=spec.source_kind == "feed")
            }
        )
        await run_file_io(lambda: self._anchors(spec))
        async with self.locks.setdefault(spec.watch_id, asyncio.Lock()):

            def commit(tx):
                existing = tx.read("watch:" + spec.watch_id)
                if existing is not None:
                    value = dict(json_value(existing.value))
                    if WatchSpec.model_validate(value["spec"]).model_dump(
                        mode="json"
                    ) != spec.model_dump(mode="json"):
                        raise ValueError("跟进标识已用于其他配置")
                    return value
                if len(self._watches()) >= MAX_WATCHES:
                    raise ValueError("最多创建 100 个跟进")
                value = {
                    "spec": spec.model_dump(mode="json"),
                    "enabled": True,
                    "created_at": datetime.now(UTC).isoformat(),
                    "project": self.catalog.attributes(spec.session_id).dimension(
                        "project"
                    ),
                }
                tx.save("watch:" + spec.watch_id, value, expected_version=None)
                return value

            result = await self.store.transact_async(commit)
        self.changed.set()
        return result

    async def set_enabled(self, request: SetEnabled):
        async with self.locks.setdefault(request.watch_id, asyncio.Lock()):
            value = await run_file_io(lambda: self._watch(request.watch_id))
            value["enabled"] = request.enabled
            await self._save("watch:" + request.watch_id, value)
        self.changed.set()
        return value

    async def _finish(self, watch_id, intent):
        """A committed source receipt wins even if the feed changed after a crash."""

        def commit(tx):
            for ref in intent["refs"]:
                key = f"event:{watch_id}:{ref['item_id']}:{ref['revision']}"
                if tx.read(key) is None:
                    value = {**ref, "observed_at": intent["observed_at"]}
                    tx.save(key, value, expected_version=None)
                    tx.save(
                        f"recent:{watch_id}:{intent['observed_at']}:{ref['item_id']}:{ref['revision']}",
                        value,
                        expected_version=None,
                    )
            current = tx.read("cursor:" + watch_id)
            tx.save(
                "cursor:" + watch_id,
                intent["cursor"],
                expected_version=None if current is None else current.version,
            )
            pending = tx.read("intent:" + watch_id)
            tx.save(
                "intent:" + watch_id,
                {},
                expected_version=None if pending is None else pending.version,
            )

        await self.store.transact_async(commit)

    async def _recover(self, watch_id, source):
        pending = await run_file_io(lambda: self.store.read("intent:" + watch_id))
        if pending and pending.value:
            intent = dict(json_value(pending.value))
            if await source.inspect_submission(intent["batch_id"]) is not None:
                await self._finish(watch_id, intent)

    async def poll(self, watch_id):
        async with self.locks.setdefault(watch_id, asyncio.Lock()):
            watch = await run_file_io(lambda: self._watch(watch_id))
            source = await self._source(watch_id)
            await self._recover(watch_id, source)
            if not watch["enabled"]:
                return {"status": "paused", "submitted": 0}
            spec = WatchSpec.model_validate(watch["spec"])
            cursor_record = await run_file_io(
                lambda: self.store.read("cursor:" + watch_id)
            )
            cursor = (
                {} if cursor_record is None else dict(json_value(cursor_record.value))
            )
            now = datetime.now(UTC)
            try:
                headers = {}
                if cursor.get("initialized"):
                    headers = {
                        key: cursor[value]
                        for key, value in (
                            ("If-None-Match", "etag"),
                            ("If-Modified-Since", "modified"),
                        )
                        if cursor.get(value)
                    }
                async with self.http.stream(
                    "GET", spec.url, headers=headers
                ) as response:
                    if response.status_code == 304:
                        if not cursor.get("initialized"):
                            raise ValueError("信息源在首次读取前返回 304")
                        cursor.update(
                            last_checked=now.isoformat(),
                            last_error=None,
                            failures=0,
                            next_check=(
                                now + timedelta(seconds=spec.interval_seconds)
                            ).isoformat(),
                        )
                        await self._save("cursor:" + watch_id, cursor)
                        return {"status": "unchanged", "submitted": 0}
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError("信息源超过 1 MiB")
                    page = None
                    if spec.source_kind == "webpage":
                        media_type = (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .lower()
                        )
                        if media_type not in {"text/html", "application/xhtml+xml"}:
                            raise ValueError("网页跟进需要 HTML；本次不推进基线")
                        page = webpages.snapshot(
                            bytes(data), str(response.url), spec.xpath
                        )
                        entries = (webpages.change(cursor.get("page"), page, spec.url),)
                    else:
                        entries = decode(bytes(data), str(response.url))
                    etag, modified = (
                        response.headers.get("etag"),
                        response.headers.get("last-modified"),
                    )
                seen = list(cursor.get("seen", []))
                baseline = cursor.get("baseline", [])
                known = set(seen) | set(baseline)
                fresh = [
                    entry
                    for entry in entries
                    if entry.item_id + ":" + entry.revision not in known
                ]
                if page is not None:
                    # A -> B -> A is a new transition even though A was seen before.
                    old_page = cursor.get("page")
                    fresh = (
                        list(entries)
                        if old_page is not None and old_page["sha256"] != page["sha256"]
                        else []
                    )
                pairs = [entry.item_id + ":" + entry.revision for entry in entries]
                checkpoint = {
                    "initialized": True,
                    "baseline": baseline if cursor.get("initialized") else pairs,
                    "seen": list(dict.fromkeys([*seen, *pairs]))[-4096:],
                    "etag": etag,
                    "modified": modified,
                    "last_checked": now.isoformat(),
                    "last_error": None,
                    "failures": 0,
                    "next_check": (
                        now + timedelta(seconds=spec.interval_seconds)
                    ).isoformat(),
                }
                if page is not None:
                    checkpoint["page"] = page
                    if fresh:
                        # Revision identifies the transition, including a genuine revert.
                        entries_revision = fingerprint(
                            [page["sha256"], cursor.get("page_transition", 0) + 1]
                        )
                        fresh = [
                            Entry(entry.item_id, entries_revision, entry.payload)
                            for entry in fresh
                        ]
                        checkpoint["page_transition"] = (
                            cursor.get("page_transition", 0) + 1
                        )
                    else:
                        checkpoint["page_transition"] = cursor.get("page_transition", 0)
                if not cursor.get("initialized"):
                    await self._save("cursor:" + watch_id, checkpoint)
                    return {
                        "status": "baseline",
                        "submitted": 0,
                        "existing_entries": len(entries),
                    }
                if not fresh:
                    await self._save("cursor:" + watch_id, checkpoint)
                    return {"status": "unchanged", "submitted": 0}
                anchors = await run_file_io(lambda: self._anchors(spec))
                context = {
                    "name": spec.name,
                    "purpose": spec.purpose,
                    "project": watch["project"],
                    "original_messages": [
                        {
                            key: anchor[key]
                            for key in ("message_id", "session_id", "recorded_at")
                        }
                        for anchor in anchors
                    ],
                    "created_at": watch["created_at"],
                }
                items = [
                    {
                        "item_id": entry.item_id,
                        "revision": entry.revision,
                        "requires_ack": False,
                        "payload": {
                            **entry.payload,
                            "source_name": spec.name,
                            "follow_up": context,
                            "context_session_id": spec.session_id,
                            **(
                                {"review_policy": REQUESTED_REVIEW}
                                if spec.review_mode == "each_update"
                                else {}
                            ),
                        },
                    }
                    for entry in fresh
                ]
                intent = {
                    "batch_id": fingerprint(
                        [[item["item_id"], item["revision"]] for item in items]
                    ),
                    "refs": [
                        {"item_id": item["item_id"], "revision": item["revision"]}
                        for item in items
                    ],
                    "cursor": checkpoint,
                    "observed_at": now.isoformat(),
                }
                # Only references are checkpointed here. Accepted payloads live in EventMail.
                await self._save("intent:" + watch_id, intent)
                await source.submit(intent["batch_id"], items)
                await self._finish(watch_id, intent)
                return {
                    "status": "submitted",
                    "submitted": len(items),
                    "refs": intent["refs"],
                }
            except (httpx.HTTPError, ValueError, etree.XMLSyntaxError) as error:
                cursor.update(
                    last_checked=now.isoformat(),
                    last_error=type(error).__name__,
                    failures=int(cursor.get("failures", 0)) + 1,
                )
                delay = min(
                    3600,
                    max(60, spec.interval_seconds)
                    * 2 ** min(cursor["failures"] - 1, 5),
                )
                cursor["next_check"] = (now + timedelta(seconds=delay)).isoformat()
                await self._save("cursor:" + watch_id, cursor)
                return {
                    "status": "error",
                    "error": cursor["last_error"],
                    "submitted": 0,
                }

    def _investigation(self, history):
        accepted = [
            change["detail"].get("accepted_turn")
            for change in history
            if change["action"] == "selected"
        ]
        decisions, evidence = [], []
        for turn in accepted:
            if not isinstance(turn, dict) or not self.catalog.exists(
                turn["session_id"]
            ):
                continue
            messages = self.catalog.reader(turn["session_id"]).snapshot()
            by_id = {message.message_id: message for message in messages}
            for message in messages:
                result = message.body
                if (
                    not isinstance(result, ToolResult)
                    or result.outcome != "success"
                    or message.source != "wake"
                ):
                    continue
                caller = by_id.get(result.call_ref.message_id)
                if caller is None or not isinstance(caller.body, Output):
                    continue
                call = caller.body.parts[result.call_ref.part_index]
                if not isinstance(call, ToolCall):
                    continue
                tool = self.bindings.describe(call.binding_id, TOOLS).get("tool", {})
                if tool.get("owner") == "wake" and tool.get("name") in {
                    "screen_content",
                    "share_content",
                    "skip_content",
                }:
                    decisions.append(
                        {
                            "tool": tool["name"],
                            "arguments": json_value(call.arguments),
                            "recorded_at": message.recorded_at.isoformat(),
                            "message_id": message.message_id,
                        }
                    )
                if tool.get("owner") == "akasha":
                    for part in result.parts:
                        if part.kind == "akasha.recall":
                            value = json_value(part.value)
                            identity = value.get("retrieval_ref")
                            record = self.recalls().read(identity) if identity else None
                            if record is not None:
                                spans = {
                                    span.message_id: span
                                    for span in record.presented_spans
                                }
                                sessions = {
                                    identity: hit.session_id
                                    for hit in record.hits
                                    for identity in hit.message_ids
                                }
                                for identity in record.presented_message_ids:
                                    original = self.catalog.reader(
                                        sessions[identity]
                                    ).get(identity)
                                    if original is None:
                                        continue
                                    span = spans.get(identity)
                                    body = text(original)
                                    excerpt = (
                                        body
                                        if span is None
                                        else body[span.start : span.end]
                                    )
                                    evidence.append(
                                        {
                                            "retrieval_ref": value["retrieval_ref"],
                                            "message_id": identity,
                                            "recorded_at": original.recorded_at.isoformat(),
                                            "text": excerpt[:2000],
                                            "kind": "recall",
                                            "truncated": len(excerpt) > 2000,
                                        }
                                    )
                        elif part.kind == "akasha.source":
                            proof = json_value(part.value)
                            parent = self.recalls().read(proof["retrieval_ref"])
                            span = proof["span"]
                            session = (
                                next(
                                    (
                                        hit.session_id
                                        for hit in parent.hits
                                        if span["message_id"] in hit.message_ids
                                    ),
                                    None,
                                )
                                if parent
                                else None
                            )
                            original = (
                                self.catalog.reader(session).get(span["message_id"])
                                if session
                                else None
                            )
                            excerpt = (
                                text(original)[span["start"] : span["end"]]
                                if original
                                else ""
                            )
                            evidence.append(
                                {
                                    "kind": "source_read",
                                    "retrieval_ref": proof["retrieval_ref"],
                                    **span,
                                    "text": excerpt[:2000],
                                    "truncated": len(excerpt) > 2000,
                                    "recorded_at": original.recorded_at.isoformat()
                                    if original
                                    else None,
                                }
                            )
        return decisions, evidence

    async def timeline(self, watch_id):
        watch = await run_file_io(lambda: self._watch(watch_id))
        source = await self._source(watch_id)
        async with self.locks.setdefault(watch_id, asyncio.Lock()):
            await self._recover(watch_id, source)

        def read():
            prefix = "recent:" + watch_id + ":"
            rows = [
                dict(json_value(record.value))
                for _, record in self.store.scan(
                    start=prefix, stop=prefix + "\uffff", limit=100
                )
            ]
            cursor = self.store.read("cursor:" + watch_id)
            return rows, cursor

        refs, cursor = await run_file_io(read)
        events = []
        for ref in refs:
            revision = await source.inspect_revision(ref["item_id"], ref["revision"])
            if revision is None:
                raise RuntimeError("已接纳的跟进条目缺少来源凭据")
            decisions, evidence = await run_file_io(
                lambda revision=revision: self._investigation(revision["history"])
            )
            identity = f"followups:{watch_id}:{ref['item_id']}:{ref['revision']}"
            receipts = []
            for settlement in revision["settlements"]:
                item = await run_file_io(
                    lambda settlement=settlement: self.delivery.confirmed(
                        settlement["settlement_ref"]
                    )
                )
                if item is not None:
                    receipts.append(
                        {
                            "message_id": item.message.message_id,
                            "confirmed_at": item.confirmed_at.isoformat(),
                            "text": text(item.message),
                        }
                    )
            feedback = await run_file_io(
                lambda identity=identity: self.store.read("feedback:" + identity)
            )
            events.append(
                {
                    **ref,
                    "payload": json_value(revision["payload"]),
                    "status": revision["status"],
                    "history": json_value(revision["history"]),
                    "decisions": decisions,
                    "evidence": evidence,
                    "deliveries": receipts,
                    "feedback": json_value(feedback.value) if feedback else None,
                }
            )
        return {
            "watch": watch,
            "anchors": await run_file_io(
                lambda: self._anchors(WatchSpec.model_validate(watch["spec"]))
            ),
            "cursor": json_value(cursor.value) if cursor else None,
            "events": events,
        }

    async def feedback(self, request: Feedback):
        timeline = await self.timeline(request.watch_id)
        event = next(
            (
                event
                for event in timeline["events"]
                if event["item_id"] == request.item_id
                and event["revision"] == request.revision
            ),
            None,
        )
        if event is None or not event["deliveries"]:
            raise ValueError("只能评价已经确认送达的跟进消息")
        identity = f"followups:{request.watch_id}:{request.item_id}:{request.revision}"

        def commit(tx):
            current = tx.read("feedback:" + identity)
            if current and current.value["value"] == request.value:
                return dict(json_value(current.value))
            value = {
                "value": request.value,
                "recorded_at": datetime.now(UTC).isoformat(),
                "delivery_message_ids": [
                    delivery["message_id"] for delivery in event["deliveries"]
                ],
            }
            tx.save(
                "feedback:" + identity,
                value,
                expected_version=current.version if current else None,
            )
            return value

        return await self.store.transact_async(commit)

    async def follow(self):
        while True:
            self.changed.clear()
            watches = await run_file_io(self._watches)
            now = datetime.now(UTC)
            deadline = now + timedelta(seconds=60)
            for watch in watches:
                if not watch["enabled"]:
                    continue
                identity = watch["spec"]["watch_id"]
                record = await run_file_io(
                    lambda identity=identity: self.store.read("cursor:" + identity)
                )
                due = (
                    datetime.fromisoformat(record.value["next_check"])
                    if record and record.value.get("next_check")
                    else now
                )
                if due <= now:
                    await self.poll(identity)
                else:
                    deadline = min(deadline, due)
            try:
                await asyncio.wait_for(
                    self.changed.wait(),
                    max(0.1, (deadline - datetime.now(UTC)).total_seconds()),
                )
            except TimeoutError:
                pass
