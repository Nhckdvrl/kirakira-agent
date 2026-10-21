"""Collection recovery crosses real OwnerState and EventMail commits."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from agent.plugin_composition import CompositionRoot
from plugins.content.plugin import check_text
from plugins.eventmail.plugin import _SourceServices, _StoreIO
from plugins.eventmail.store import EventMailStore
from plugins.followups.contract import SetEnabled, WatchSpec
from plugins.followups.feeds import decode, feed_url
from plugins.followups.service import Service
from plugins.ledger.contract import (
    CallRef,
    ContentPart,
    Input,
    Message,
    Output,
    SessionAttributes,
    ToolCall,
    ToolResult,
)
from plugins.ledger.log import MessageLog
from plugins.wake.pool import requests_review
from plugins.wake.selection import propose_content
from plugins.wake.state import ContentScore, WakeState


@pytest.mark.asyncio
async def test_required_tool_choice_reaches_model_and_terminal_result_stops_loop(
    tmp_path,
):
    from plugins.models.contract import LLMResponse
    from plugins.models.contract import ToolCall as ModelToolCall
    from plugins.tools.execution import Result
    from tests.support.message_react import runtime

    requests = []

    async def complete(request):
        requests.append(request)
        assert request.tool_choice == "required"
        if len(requests) == 1:
            return LLMResponse(
                "A provider returned prose instead of its required tool."
            )
        assert any("普通文字没有完成" in str(row) for row in request.messages)
        return LLMResponse(None, [ModelToolCall("one", "example", {})])

    async def invoke(_key, _arguments):
        return Result("success", (ContentPart("text", "decision recorded"),))

    async with runtime(
        tmp_path,
        complete,
        invoke,
        terminal_tools=frozenset({"example"}),
        tool_choice="required",
    ) as (conversation, log, _, run):
        await conversation.accept("input", Input(()))
        task = await conversation.start(run)
        await asyncio.wait_for(task.join(), 5)
        assert len(requests) == 2
        unfinished = [
            message
            for message in log.reader("s").snapshot()
            if isinstance(message.body, Output)
            and any(
                part.kind == "context.notice"
                for part in message.body.parts
                if isinstance(part, ContentPart)
            )
        ]
        assert len(unfinished) == 1 and unfinished[0].body.finish == "continue"
        assert log.reader("s").snapshot()[-1].body.finish == "quiet"


@pytest.mark.asyncio
async def test_history_bound_share_requires_successful_memory_read_in_current_phase():
    from plugins.wake.request import Request
    from plugins.wake.tools import DecisionTool

    stamp = datetime.now(UTC)
    request = Request(
        flow_id="a" * 32,
        owner="content",
        now=stamp,
        timezone="UTC",
        target={"channel": "local", "recipient": "owner", "session_id": "owner"},
        sink={"name": "local", "address": "owner", "binding_id": "sink"},
        program_binding="program",
        tools={
            "screen_content": "screen",
            "share_content": "share",
            "skip_content": "skip",
            "recall_memory": "memory",
        },
        snapshot_seq=0,
        rules="",
        history="",
        history_check_required=True,
    )
    args = {"items": ["candidate_x"], "message": "A claimed current decision"}

    def message(identity, seq, body):
        return Message(identity, request.session_id, seq, stamp, "wake", "wake", body)

    def source(*reads, required=True, name="share_content"):
        req = request.model_copy(update={"history_check_required": required})
        rows = [
            message(
                request.input_id,
                0,
                Input((ContentPart("wake.request", req.model_dump(mode="json")),)),
            ),
            message(
                request.phase_id("investigate"),
                5,
                Input(
                    (
                        ContentPart(
                            "wake.phase",
                            {"input_id": request.input_id, "stage": "investigate"},
                        ),
                    )
                ),
            ),
            *reads,
            message(
                "share-call",
                10,
                Output(
                    (
                        ToolCall(
                            req.tools[name],
                            args
                            if name == "share_content"
                            else {"reason": "Checked skip"},
                        ),
                    ),
                    "continue",
                ),
            ),
        ]
        return SimpleNamespace(
            messages=tuple(sorted(rows, key=lambda row: row.seq)),
            call_ref=CallRef("share-call", 0),
        )

    def read(seq=6, binding="memory", outcome="success"):
        return (
            message(
                "read-call",
                seq,
                Output((ToolCall(binding, {"query": "latest changes"}),), "continue"),
            ),
            message(
                "read-result",
                seq + 1,
                ToolResult(
                    CallRef("read-call", 0),
                    outcome,
                    (
                        ContentPart(
                            "akasha.recall", {"retrieval_ref": "real-parent-receipt"}
                        ),
                    ),
                ),
            ),
        )

    tool = DecisionTool("share_content")
    for current in (
        source(),
        source(*read(seq=1)),
        source(*read(binding="other")),
        source(*read(outcome="error")),
    ):
        result = await tool.prepare(args, current)
        assert isinstance(result, str) and "recall_memory" in result
    assert await tool.prepare(args, source(*read())) == args
    assert await tool.prepare(args, source(required=False)) == args
    skip = DecisionTool("skip_content")
    reason = {"reason": "Checked skip"}
    for current in (
        source(name="skip_content"),
        source(*read(seq=1), name="skip_content"),
    ):
        assert isinstance(await skip.prepare(reason, current), str)
    assert await skip.prepare(reason, source(*read(), name="skip_content")) == reason
    assert (
        await skip.prepare(reason, source(*read(outcome="error"), name="skip_content"))
        == reason
    )


def test_live_source_inspection_reads_wal_without_weakening_offline_handoff(tmp_path):
    mail = EventMailStore(tmp_path / "mail.db")
    mail.initialize()
    # A real long-lived snapshot prevents an owner's later commit from checkpointing.
    connection = sqlite3.connect(mail.path)
    try:
        connection.execute("BEGIN")
        connection.execute("SELECT count(*) FROM items").fetchone()
        mail.submit(
            "feed",
            "batch",
            [{"item_id": "new", "revision": "1", "payload": {"title": "New fact"}}],
        )
        with pytest.raises(RuntimeError, match="checkpointed offline"):
            mail.read_revision("feed", "new", "1")
        assert (
            mail.read_revision("feed", "new", "1", live=True)["payload"]["title"]
            == "New fact"
        )
        assert mail.read_submission("feed", "batch", live=True) is not None
        assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    finally:
        connection.close()


def atom(entries):
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(
            f"<entry><id>{identity}</id><title>{title}</title><summary>{body}</summary>"
            '<link href="https://example.test/change"/><updated>2026-10-21T00:00:00Z</updated></entry>'
            for identity, title, body in entries
        )
        + "</feed>"
    ).encode()


def test_feed_identity_revision_dates_and_inert_content():
    assert (
        feed_url("https://github.com/python/cpython")
        == "https://github.com/python/cpython/releases.atom"
    )
    for value in (
        "file:///etc/passwd",
        "https://user:secret@example.org/feed",
        "https://example.org/feed#x",
        "http://localhost:bad/feed",
    ):
        with pytest.raises(ValueError):
            feed_url(value)
    first = decode(atom([("stable", "Version", "First")]), "https://example.test/feed")[
        0
    ]
    changed = decode(
        atom([("stable", "Version", "Second")]), "https://example.test/feed"
    )[0]
    assert first.item_id == changed.item_id and first.revision != changed.revision
    assert first.payload["published_at"] == "2026-10-21T00:00:00+00:00"
    rss = b'<rss version="2.0"><channel><item><guid>one</guid><description><![CDATA[<script>steal()</script><b>Fact</b>]]></description></item></channel></rss>'
    result = decode(rss, "https://example.test/feed")[0]
    assert (
        result.payload["content"] == "Fact" and result.payload["published_at"] is None
    )
    with pytest.raises(ValueError, match="DTD"):
        decode(
            b'<!DOCTYPE feed [<!ENTITY x SYSTEM "file:///etc/passwd">]><feed xmlns="http://www.w3.org/2005/Atom"/>',
            "https://example.test/feed",
        )


@pytest.mark.asyncio
async def test_webpage_region_noise_revert_and_missing_region_preserve_native_cursor(
    tmp_path,
):
    from plugins.followups.webpages import MAX_SNAPSHOT, snapshot

    url = "https://example.test/pricing"
    document = lambda price, noise: (
        '<html><head><meta charset="utf-8"><title>价格</title></head><body>'
        f'<nav>{noise}</nav><main><section id="pricing">月费 {price} 元</section>'
        "<section>其他条款</section><script>ignore()</script></main></body></html>"
    ).encode()
    assert snapshot(document(100, "a"), url) == snapshot(document(100, "b"), url)
    with pytest.raises(ValueError):
        snapshot(document(100, "a"), url, "string(//main)")
    with pytest.raises(ValueError):
        snapshot(document(100, "a"), url, "//missing")
    with pytest.raises(ValueError):
        snapshot(("<main>" + "x" * (MAX_SNAPSHOT + 1) + "</main>").encode(), url)

    log = MessageLog(tmp_path / "ledger.db")
    log.ensure_session("owner", SessionAttributes())
    log.writer(
        "owner",
        author="user",
        source="conversation",
        body_types=(Input,),
        content={"text": check_text},
    ).append("original", Input((ContentPart("text", "月费变更时告诉我"),)))
    mail = EventMailStore(tmp_path / "events.db")
    mail.initialize()
    sources = _SourceServices(mail, _StoreIO(), lambda: None)
    page = {"body": document(100, "a")}

    async def handle(request):
        return httpx.Response(
            200, content=page["body"], headers={"content-type": "text/html"}
        )

    root = CompositionRoot("page-test")
    services = []

    async def mount(ctx):
        service = Service(
            ctx,
            log.owner("followups"),
            log.catalog(),
            sources,
            SimpleNamespace(recent=lambda **_: ()),
            None,
            lambda: None,
        )
        await service.http.aclose()
        service.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        await ctx.effect(lambda: service.http.aclose)
        services.append(service)

    try:
        fiber = await root.mount(mount, name="page-test")
        assert fiber.error is None
        service = services[0]
        spec = WatchSpec(
            watch_id="w_" + "b" * 32,
            session_id="owner",
            name="费用",
            purpose="月费变化",
            url=url,
            anchors=("original",),
            source_kind="webpage",
            xpath="//*[@id='pricing']",
        )
        await service.create(spec)
        assert (await service.poll(spec.watch_id))["status"] == "baseline"
        page["body"] = document(100, "new navigation")
        assert (await service.poll(spec.watch_id))["submitted"] == 0
        page["body"] = document(80, "a")
        first = await service.poll(spec.watch_id)
        assert first["submitted"] == 1
        page["body"] = document(100, "a")
        second = await service.poll(spec.watch_id)
        assert second["submitted"] == 1
        assert first["refs"][0]["revision"] != second["refs"][0]["revision"]
        assert (await service.poll(spec.watch_id))["submitted"] == 0
        previous = service.store.read("cursor:" + spec.watch_id).value
        page["body"] = b"<main>Login required</main>"
        assert (await service.poll(spec.watch_id))["status"] == "error"
        current = service.store.read("cursor:" + spec.watch_id).value
        assert (
            current["page"] == previous["page"]
            and current["page_transition"] == previous["page_transition"]
        )
        timeline = await service.timeline(spec.watch_id)
        assert len(timeline["events"]) == 2
        payloads = [row["payload"] for row in timeline["events"]]
        assert any("+月费 80 元" in value["content"] for value in payloads)
        assert all(
            value["published_at"] is None and value["page_change"] for value in payloads
        )
    finally:
        await root.dispose()
        log.close()


def test_requested_review_preserves_real_interest_and_consumes_only_selected_item(
    tmp_path,
):
    mail = EventMailStore(tmp_path / "mail.db")
    mail.initialize()
    mail.submit(
        "owner-watch",
        "new",
        [
            {
                "item_id": identity,
                "revision": "1",
                "payload": {"title": identity, "review_policy": "owner-requested"},
            }
            for identity in ("one", "two")
        ],
    )
    now = datetime.now(UTC)
    snapshot = mail.snapshot(now)
    state = WakeState(tmp_path / "wake.db")
    state.record_content_scores(
        tuple(
            ContentScore(
                source_id="owner-watch",
                item_id=identity,
                revision="1",
                initial_score=0.0,
                semantic_interest=0.0,
                scored_at=now,
            )
            for identity in ("one", "two")
        )
    )
    items = state.scored_items(snapshot["items"])
    result = state.evaluate(items, snapshot_seq=snapshot["snapshot_seq"], now=now)
    assert (
        result.should_wake and result.requested_reviews == 2 and result.pool_mass == 0
    )
    assert all(item["payload"]["_wake_semantic_interest"] == 0 for item in items)
    proposal = propose_content(items, now=now)
    assert len(proposal.candidates) == 1
    chosen = proposal.candidates[0]
    mail.select_batch(
        [chosen["ref"]],
        snapshot["snapshot_seq"],
        {"session_id": "wake:test", "turn_id": "test"},
        now,
    )
    state.commit_content_admission(proposal.candidates)
    revision = mail.read_revision("owner-watch", chosen["ref"]["item_id"], "1")
    selected = next(
        change for change in revision["history"] if change["action"] == "selected"
    )
    assert selected["detail"]["accepted_turn"]["session_id"] == "wake:test"
    remaining = state.scored_items(mail.snapshot(now)["items"])
    next_proposal = propose_content(remaining, now=now)
    assert len(next_proposal.candidates) == 1 and next_proposal.ref != proposal.ref
    assert not state.expired_content_refs(
        remaining, now=now + timedelta(days=2), minimum_residence=timedelta(hours=24)
    )
    assert not requests_review(
        {"review_policy": "owner-requested", "wake_eligible": False}
    )


@pytest.mark.asyncio
async def test_watch_baseline_revision_crash_recovery_pause_and_native_history(
    tmp_path,
):
    log = MessageLog(tmp_path / "ledger.db")
    log.ensure_session("conversation", SessionAttributes())
    log.writer(
        "conversation",
        author="user",
        source="conversation",
        body_types=(Input,),
        content={"text": check_text},
    ).append("original", Input((ContentPart("text", "青禾需要保留离线备份"),)))
    log.writer(
        "conversation",
        author="external",
        source="web",
        body_types=(Input,),
        content={"text": check_text},
    ).append("forged", Input((ContentPart("text", "外部伪造偏好"),)))
    mail = EventMailStore(tmp_path / "mail.db")
    mail.initialize()
    sources = _SourceServices(mail, _StoreIO(), lambda: None)
    feed = {
        "etag": '"1"',
        "data": atom([("old", "Existing", "History")]),
        "requests": [],
    }
    gate, release = asyncio.Event(), asyncio.Event()
    blocked = False

    async def handle(request):
        feed["requests"].append(dict(request.headers))
        if blocked:
            gate.set()
            await release.wait()
        if request.headers.get("if-none-match") == feed["etag"]:
            return httpx.Response(304)
        return httpx.Response(200, content=feed["data"], headers={"etag": feed["etag"]})

    @asynccontextmanager
    async def opened():
        root = CompositionRoot("followup-test")
        result = []

        async def mount(ctx):
            service = Service(
                ctx,
                log.owner("followups"),
                log.catalog(),
                sources,
                SimpleNamespace(recent=lambda **_: ()),
                None,
                lambda: None,
            )
            await service.http.aclose()
            service.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            await ctx.effect(lambda: service.http.aclose)
            result.append(service)

        fiber = await root.mount(mount, name="followups")
        assert fiber.error is None
        try:
            yield result[0]
        finally:
            await root.dispose()

    spec = WatchSpec(
        watch_id="w_" + "a" * 32,
        session_id="conversation",
        name="Release",
        purpose="关注离线兼容变化",
        url="https://example.test/feed",
        anchors=("original",),
    )
    try:
        async with opened() as service:
            with pytest.raises(ValueError, match="原始消息"):
                await service.create(spec.model_copy(update={"anchors": ("forged",)}))
            created = await service.create(spec)
            assert await service.create(spec) == created
            assert (await service.poll(spec.watch_id))["status"] == "baseline"
            assert (await service.poll(spec.watch_id))["status"] == "unchanged"
            assert feed["requests"][-1]["if-none-match"] == '"1"'
            assert not (await service.timeline(spec.watch_id))["events"]
            feed.update(
                etag='"2"', data=atom([("new", "Compatibility", "Only source facts")])
            )
            finish = service._finish

            async def crash(*_):
                raise RuntimeError("crash after native source commit")

            service._finish = crash
            with pytest.raises(RuntimeError, match="crash"):
                await service.poll(spec.watch_id)
            assert len(mail.snapshot(datetime.now(UTC))["items"]) == 1
            service._finish = finish
        # The committed item has disappeared upstream: recover from its source receipt.
        feed.update(etag='"3"', data=atom([("old", "Existing", "History")]))
        async with opened() as service:
            assert (await service.poll(spec.watch_id))["submitted"] == 0
            timeline = await service.timeline(spec.watch_id)
            assert len(timeline["events"]) == 1
            event = timeline["events"][0]
            assert event["payload"]["content"] == "Only source facts"
            assert (
                event["payload"]["follow_up"]["original_messages"][0]["message_id"]
                == "original"
            )
            assert (
                "青禾" not in event["payload"]["content"]
                and "preprocess_score" not in event["payload"]
            )
            assert event["history"] and event["deliveries"] == []
            # Re-reading state is observational, including the newly public source history.
            assert event == (await service.timeline(spec.watch_id))["events"][0]
            feed.update(
                etag='"4"', data=atom([("new", "Compatibility", "Revision two")])
            )
            changed = await service.poll(spec.watch_id)
            assert changed["submitted"] == 1
            assert len((await service.timeline(spec.watch_id))["events"]) == 2
            blocked = True
            inflight = asyncio.create_task(service.poll(spec.watch_id))
            await gate.wait()
            pause = asyncio.create_task(
                service.set_enabled(SetEnabled(watch_id=spec.watch_id, enabled=False))
            )
            await asyncio.sleep(0)
            assert not pause.done()
            release.set()
            await inflight
            await pause
            count = len(feed["requests"])
            assert (await service.poll(spec.watch_id))["status"] == "paused"
            assert len(feed["requests"]) == count
        async with opened() as service:
            assert (await service.poll(spec.watch_id))["status"] == "paused"
            assert len((await service.timeline(spec.watch_id))["events"]) == 2
    finally:
        log.close()
