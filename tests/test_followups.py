"""Collection recovery crosses real OwnerState and EventMail commits."""

import asyncio
import json
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
    json_value,
)
from plugins.ledger.log import MessageLog
from plugins.wake.pool import requests_review
from plugins.wake.selection import propose_content
from plugins.wake.state import ContentScore, WakeState


@pytest.mark.asyncio
async def test_chat_followup_scope_atomic_receipts_and_original_rule_survive_replay(
    tmp_path,
):
    from plugins.followups.contract import check_policy_record
    from plugins.followups.tools import FollowupTool
    from plugins.tools.contract import CallSource

    log = MessageLog(tmp_path / "chat.db")
    for session in ("owner", "other"):
        log.ensure_session(session, SessionAttributes())

    def writer(session="owner", source="conversation"):
        return log.writer(
            session,
            author="user",
            source=source,
            body_types=(Input,),
            content={"text": check_text, "followups.policy": check_policy_record},
        )

    original = writer().append(
        "request", Input((ContentPart("text", "盯着公告，周末开放才告诉我"),))
    )
    revised = writer().append(
        "correction", Input((ContentPart("text", "之后只有票价低于90元才通知"),))
    )
    foreign = writer("other").append(
        "foreign", Input((ContentPart("text", "暂停跟进"),))
    )

    def source(identity, user=original, source_name="conversation"):
        calling = Message(
            identity,
            user.session_id,
            user.seq + 1,
            datetime.now(UTC),
            "assistant",
            source_name,
            Output((ToolCall("binding", {}),), "continue"),
        )
        return CallSource(CallRef(identity, 0), (user, calling))

    root, services = CompositionRoot("chat-followup-test"), []

    async def mount(ctx):
        service = Service(
            ctx,
            log.owner("followups"),
            log.catalog(),
            None,
            None,
            None,
            None,
            writers=lambda sid: writer(sid, "followups"),
        )
        await ctx.effect(lambda: service.http.aclose)
        services.append(service)

    try:
        fiber = await root.mount(mount, name="followups")
        assert fiber.error is None
        service = services[0]

        async def ready():
            return {
                "enabled": True,
                "ready": True,
                "reason": "",
                "values": {"delivery": {"session_id": "owner", "channel": "local"}},
            }

        create, manage, read = (
            FollowupTool(service, name, ready) for name in ("create", "manage", "read")
        )
        args = {
            "name": "展览",
            "purpose": "周末开放才告诉我",
            "source_kind": "webpage",
            "url": "https://example.test/exhibit",
        }
        # Historical mail access is opt-in, bounded, and cannot be attached to
        # unrelated webpage/feed watches or silently accept bool/string limits.
        for limit in (True, "12", -1, 51, 1):
            assert isinstance(await create.prepare(
                {**args, "mail_initial_limit": limit}, source("bad-history")), str)
        mail_request = writer().append("mail-request", Input((ContentPart(
            "text", "请回查 Junk 最近12封邮件，只在需要补材料时通知我。"),)))
        mail_prepared = await create.prepare({
            "name": "重要邮件", "purpose": "只在需要补材料时通知我",
            "source_kind": "mailbox", "mailbox_id": "ma_" + "a" * 32,
            "mail_folder": "Junk", "mail_initial_limit": 12,
        }, source("mail-create", mail_request))
        assert mail_prepared["spec"]["mail_initial_limit"] == 12
        assert mail_prepared["spec"]["mail_folder"] == "Junk"
        assert mail_prepared["spec"]["anchors"] == [mail_request.message_id]
        assert isinstance(
            await FollowupTool(service, "create").prepare(args, source("unconfigured")),
            str,
        )
        assert not (await service.overview("owner"))["watches"]
        assert isinstance(
            await create.prepare({**args, "purpose": "每天无条件通知"}, source("bad")),
            str,
        )
        assert isinstance(
            await create.prepare(
                args, source("background", source_name="scheduler:one")
            ),
            str,
        )
        prepared = await create.prepare(args, source("create"))
        key = source("create").effect_key
        created = await create.invoke(key, prepared)
        identity = prepared["spec"]["watch_id"]
        assert created.outcome == "success" and created == await create.query(key)
        assert created == await create.invoke(key, prepared)
        assert len((await service.overview("owner"))["watches"]) == 1
        assert (await service.overview("other"))["watches"] == []
        assert isinstance(
            await manage.prepare(
                {"watch_id": identity, "action": "pause"},
                source("foreign-call", foreign),
            ),
            str,
        )
        assert isinstance(
            await read.prepare({"watch_id": identity}, source("foreign-read", foreign)),
            str,
        )

        pause = await manage.prepare(
            {"watch_id": identity, "action": "pause"}, source("pause")
        )
        paused = await manage.invoke(source("pause").effect_key, pause)
        resume = await manage.prepare(
            {"watch_id": identity, "action": "resume"}, source("resume")
        )
        await manage.invoke(source("resume").effect_key, resume)
        # An old pause replay must not undo a newer explicit resume.
        assert paused == await manage.invoke(source("pause").effect_key, pause)
        assert service._watch(identity)["enabled"]

        revision = await manage.prepare(
            {
                "watch_id": identity,
                "action": "revise",
                "purpose": "只有票价低于90元才通知",
            },
            source("revise", revised),
        )
        transact = service.store.transact_async

        async def crash(operation):
            def commit(tx):
                operation(tx)
                raise RuntimeError("lost before commit")

            return await transact(commit)

        service.store.transact_async = crash
        with pytest.raises(RuntimeError, match="before commit"):
            await manage.invoke(source("revise", revised).effect_key, revision)
        service.store.transact_async = transact
        assert await manage.query(source("revise", revised).effect_key) is None
        assert not any(
            row.source == "followups" for row in log.reader("owner").snapshot()
        )
        saved = await manage.invoke(source("revise", revised).effect_key, revision)
        assert saved == await manage.query(source("revise", revised).effect_key)
        policy = service._policy(service._watch(identity))
        assert (
            policy["revision"] == 1
            and policy["origin_message_id"] == revised.message_id
        )
        assert policy["purpose"] == "只有票价低于90元才通知"
        assert log.reader("owner").get(original.message_id) == original
        new_pause = await manage.prepare(
            {"watch_id": identity, "action": "pause"}, source("pause-again")
        )
        stopped = await manage.invoke(source("pause-again").effect_key, new_pause)
        assert (
            json.loads(stopped.parts[0].value)["watch"]["purpose"] == policy["purpose"]
        )

        # New tool objects after owner disposal/reopen recover the same durable receipt.
        await root.dispose()
        root = CompositionRoot("chat-followup-reopened")
        fiber = await root.mount(mount, name="followups")
        assert fiber.error is None
        restored = FollowupTool(services[-1], "manage")
        assert await restored.query(source("revise", revised).effect_key) == saved
        assert not services[-1]._watch(identity)["enabled"]

    finally:
        await root.dispose()
        log.close()


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
            # Older versions recorded only the identities/checkpoint. Their
            # already committed receipt must still recover without rewriting
            # the accepted source body or requiring a new fetch of it.
            pending = dict(json_value(service.store.read("intent:" + spec.watch_id).value))
            pending.pop("items")
            await service._save("intent:" + spec.watch_id, pending)
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


@asynccontextmanager
async def monitoring_service(directory, handler):
    """Real OwnerState/EventMail with only the remote transport controlled."""
    directory.mkdir(parents=True, exist_ok=True)
    log = MessageLog(directory / "ledger.db")
    log.ensure_session("owner", SessionAttributes())
    writer = log.writer("owner", author="user", source="conversation", body_types=(Input,),
                        content={"text": check_text})
    if log.reader("owner").get("monitor-original") is None:
        writer.append("monitor-original", Input((ContentPart("text", "请盯着售票，有符合条件的票才告诉我"),)))
    mail = EventMailStore(directory / "events.db")
    mail.initialize()
    sources = _SourceServices(mail, _StoreIO(), lambda: None)
    root, services = CompositionRoot("monitor-test"), []

    async def mount(ctx):
        service = Service(ctx, log.owner("followups"), log.catalog(), sources,
                          SimpleNamespace(recent=lambda **_: ()), None, lambda: None)
        await service.http.aclose()
        service.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await ctx.effect(lambda: service.http.aclose)
        services.append(service)

    try:
        fiber = await root.mount(mount, name="followups")
        assert fiber.error is None
        yield services[0]
    finally:
        await root.dispose()
        log.close()


def monitoring_spec(index=0, **options):
    return WatchSpec(watch_id="w_" + format(index, "032x"), session_id="owner",
                     name="售票关注", purpose="有符合条件的票才告诉我", anchors=("monitor-original",),
                     url="https://example.test/" + str(index), interval_seconds=3600, **options)


async def eventually(condition, timeout=3):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_monitor_pump_isolates_slow_sources_bounds_concurrency_and_drains(tmp_path):
    from plugins.followups.service import MAX_CONCURRENT_POLLS

    gates = [asyncio.Event() for _ in range(6)]
    started, cancelled, active, peak = set(), set(), set(), 0

    async def handler(request):
        nonlocal peak
        index = int(request.url.path.strip("/"))
        started.add(index)
        active.add(index)
        peak = max(peak, len(active))
        try:
            await gates[index].wait()
            return httpx.Response(200, content=atom([]))
        except asyncio.CancelledError:
            cancelled.add(index)
            raise
        finally:
            active.discard(index)

    async with monitoring_service(tmp_path, handler) as service:
        for index in range(6):
            await service.create(monitoring_spec(index))
        pump = asyncio.create_task(service.follow())
        try:
            await eventually(lambda: len(started) == MAX_CONCURRENT_POLLS)
            assert len(active) == MAX_CONCURRENT_POLLS
            assert 0 in active
            for gate in gates[1:]:
                gate.set()
            await eventually(lambda: service.store.read("cursor:" + monitoring_spec(5).watch_id) is not None)
            assert 0 in active and len(started) == 6
            assert peak == MAX_CONCURRENT_POLLS
        finally:
            pump.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pump
        assert cancelled == {0} and not active


@pytest.mark.asyncio
async def test_monitor_signal_during_active_poll_is_coalesced_without_losing_arrival(tmp_path):
    state = {"calls": 0, "version": 1}
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        state["calls"] += 1
        version = state["version"]
        if state["calls"] == 2:
            entered.set()
            await release.wait()
        if request.headers.get("if-none-match") == str(version):
            return httpx.Response(304)
        return httpx.Response(200, content=atom([] if version == 1 else [("ticket", "有票", "可以购买")]),
                              headers={"etag": str(version)})

    async with monitoring_service(tmp_path, handler) as service:
        spec = monitoring_spec()
        await service.create(spec)
        assert (await service.poll(spec.watch_id))["status"] == "baseline"
        pump = asyncio.create_task(service.follow())
        try:
            service.request_check(spec.watch_id)
            await asyncio.wait_for(entered.wait(), 3)
            state["version"] = 2
            for _ in range(500):
                service.request_check(spec.watch_id)
            release.set()
            await eventually(lambda: state["calls"] >= 3)
            async with asyncio.timeout(3):
                while not (await service.timeline(spec.watch_id))["events"]:
                    await asyncio.sleep(0.01)
            assert state["calls"] == 3
            assert len((await service.timeline(spec.watch_id))["events"]) == 1
            await service.set_enabled(SetEnabled(watch_id=spec.watch_id, enabled=False))
            for _ in range(500):
                service.request_check(spec.watch_id)
            await asyncio.sleep(0.05)
            assert state["calls"] == 3
        finally:
            pump.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pump


@pytest.mark.asyncio
async def test_monitor_retry_after_survives_restart_and_forced_hints(tmp_path):
    from email.utils import format_datetime

    calls = 0
    retry = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": format_datetime(retry, usegmt=True)})

    spec = monitoring_spec()
    async with monitoring_service(tmp_path, handler) as service:
        await service.create(spec)
        assert (await service.poll(spec.watch_id))["status"] == "error"
        cursor = service.store.read("cursor:" + spec.watch_id).value
        assert datetime.fromisoformat(cursor["retry_not_before"]) == retry
        assert datetime.fromisoformat(cursor["next_check"]) >= retry
    async with monitoring_service(tmp_path, handler) as service:
        service.request_check(spec.watch_id)
        pump = asyncio.create_task(service.follow())
        try:
            assert (await service.poll(spec.watch_id))["status"] == "deferred"
            await asyncio.sleep(0.05)
            assert calls == 1
        finally:
            pump.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pump


@pytest.mark.asyncio
async def test_web_monitor_initial_status_button_only_opening_and_legacy_upgrade(tmp_path):
    from plugins.followups.feeds import fingerprint
    from plugins.followups.webpages import snapshot

    page = {"html": '<main><h1>音乐会</h1><button disabled>购买门票</button></main>'}

    async def handler(request):
        return httpx.Response(200, content=page["html"].encode(), headers={"content-type": "text/html"})

    async with monitoring_service(tmp_path, handler) as service:
        spec = monitoring_spec(source_kind="webpage", check_initial=True, review_mode="interest")
        await service.create(spec)
        first = await service.poll(spec.watch_id)
        assert first["submitted"] == 1
        event = (await service.timeline(spec.watch_id))["events"][0]
        assert event["payload"]["page_change"]["initial"] is True
        assert event["payload"]["review_policy"] == "owner-requested"
        assert event["payload"]["published_at"] is None
        assert (await service.poll(spec.watch_id))["submitted"] == 0
        before = snapshot(page["html"].encode(), spec.url)
        page["html"] = page["html"].replace(" disabled", "")
        after = snapshot(page["html"].encode(), spec.url)
        assert before["text"] == after["text"] and before["sha256"] != after["sha256"]
        assert (await service.poll(spec.watch_id))["submitted"] == 1
        assert len((await service.timeline(spec.watch_id))["events"]) == 2

        legacy = monitoring_spec(1, source_kind="webpage")
        await service.create(legacy)
        assert (await service.poll(legacy.watch_id))["status"] == "baseline"
        cursor = dict(service.store.read("cursor:" + legacy.watch_id).value)
        cursor["page"] = {key: after[key] for key in ("text", "title", "url")}
        cursor["page"]["sha256"] = fingerprint(after["text"])
        await service._save("cursor:" + legacy.watch_id, cursor)
        # A v1 checkpoint never observed controls: adopt v2 silently, never
        # invent historical availability from newly added state fields.
        assert (await service.poll(legacy.watch_id))["submitted"] == 0
        page["html"] = page["html"].replace("<button>", "<button disabled>")
        assert (await service.poll(legacy.watch_id))["submitted"] == 1


def test_web_monitor_controls_exclude_tokens_scripts_hidden_fields_and_obey_disabled_ancestors():
    from plugins.followups.webpages import snapshot

    url = "https://example.test/tickets"
    def page(token, disabled):
        return (
            '<html><head><meta charset="utf-8"></head><body><main>'
            '<fieldset disabled><legend><button>说明</button></legend><button>购买门票</button></fieldset>'
            f'<section aria-disabled="{disabled}"><a href="/buy?event=42&token={token}&utm_source={token}">选座</a></section>'
            f'<input type="hidden" role="button" name="csrf" value="{token}"><input type="password" role="button" value="{token}">'
            f'<input type="text" role="button" value="{token}">'
            '<a href="javascript:secret()">脚本链接</a><button onclick="secret()">查看</button>'
            '</main></body></html>'
        ).encode()

    first = snapshot(page("PRIVATE_TOKEN_A", "true"), url)
    rotated = snapshot(page("PRIVATE_TOKEN_B", "true"), url)
    enabled = snapshot(page("PRIVATE_TOKEN_B", "false"), url)
    assert first["sha256"] == rotated["sha256"] != enabled["sha256"]
    encoded = json.dumps(first, ensure_ascii=False)
    assert "PRIVATE_TOKEN" not in encoded and "secret()" not in encoded and "csrf" not in encoded
    controls = first["interactions"]
    by_label = {row["label"]: row for row in controls}
    assert by_label["说明"]["disabled"] is False
    assert by_label["购买门票"]["disabled"] is True
    assert by_label["选座"]["aria_disabled"] is True
    plain = "<main>购买门票</main>"
    assert snapshot(plain.encode("utf-8"), url)["text"] == "购买门票"
    assert snapshot(plain.encode("gb18030"), url, encoding="gb18030")["text"] == "购买门票"
    declared = '<html><head><meta charset="gb18030"></head><body>' + plain + '</body></html>'
    assert snapshot(declared.encode("gb18030"), url)["text"] == "购买门票"
    with pytest.raises(ValueError):
        snapshot(plain.encode(), url, encoding="not-a-charset")
    from plugins.followups.webpages import safe_url

    assert safe_url("/magic-link/PRIVATE_PATH_TOKEN_A", url) is None
    assert safe_url("/%256d%2561gic-link/PRIVATE_PATH_TOKEN_A", url) is None
    assert safe_url("/events/42/buy", url) == "https://example.test/events/42/buy"


def test_delivery_feedback_becomes_a_bounded_attention_weight():
    """Ratings tune how loud a watch is, within fixed bounds and only with evidence.

    An unrated or barely-rated watch must behave exactly as before, the weight
    must stay inside its bounds however one-sided the ratings are, and it must
    be monotone so one more complaint never makes a source louder.
    """
    from plugins.followups.feedback import (
        MAX_WEIGHT,
        MIN_RATINGS,
        MIN_WEIGHT,
        NEUTRAL_WEIGHT,
        measure,
    )

    def rows(useful, irrelevant):
        return [{"value": "useful"}] * useful + [{"value": "irrelevant"}] * irrelevant

    # No evidence, and not enough evidence, both stay neutral.
    assert measure([]).weight == NEUTRAL_WEIGHT
    assert not measure([]).adjusted
    assert measure(rows(MIN_RATINGS - 1, 0)).weight == NEUTRAL_WEIGHT
    assert not measure(rows(MIN_RATINGS - 1, 0)).adjusted

    # With enough evidence the extremes reach, but never exceed, the bounds.
    assert measure(rows(MIN_RATINGS, 0)).weight == MAX_WEIGHT
    assert measure(rows(0, MIN_RATINGS)).weight == MIN_WEIGHT
    assert measure(rows(50, 0)).weight == MAX_WEIGHT
    assert measure(rows(0, 50)).weight == MIN_WEIGHT
    # An even split is neutral but is a real measurement, not an absent one.
    even = measure(rows(4, 4))
    assert even.weight == NEUTRAL_WEIGHT and even.rated == 8

    # Monotone in both directions.
    irrelevant_series = [measure(rows(10, n)).weight for n in range(0, 12)]
    assert irrelevant_series == sorted(irrelevant_series, reverse=True)
    useful_series = [measure(rows(n, 10)).weight for n in range(0, 12)]
    assert useful_series == sorted(useful_series)

    # Unreadable rows are ignored rather than counted as either judgement.
    assert measure(rows(4, 0) + [{"value": "maybe"}, {}, {"value": None}]).rated == 4
    assert all(MIN_WEIGHT <= measure(rows(a, b)).weight <= MAX_WEIGHT
               for a in range(0, 9) for b in range(0, 9))


def test_attention_weight_moves_the_pool_but_never_a_requested_review():
    """The weight changes how much a rated source contributes, nothing else.

    It must not change whether an item cleared the admission floor, and it must
    not apply to a review the owner explicitly asked for.
    """
    from datetime import UTC, datetime

    from plugins.eventmail.contract import REQUESTED_REVIEW
    from plugins.wake.pool import WAKE_POOL_THRESHOLD, attention_weight, measure_pool

    now = datetime.now(UTC)

    def event(identity, *, weight=None, requested=False, initial=1.0):
        # Shaped like wake.state._event output: the Content payload is flattened
        # into the event, so a field nested under "payload" would never be seen.
        event = {"id": identity, "_wake_initial_score": initial, "source_name": "s",
                 "observed_at": now.isoformat(), "published_at": now.isoformat()}
        if weight is not None:
            event["attention_weight"] = weight
        if requested:
            event["review_policy"] = REQUESTED_REVIEW
        return event

    # A malformed or out-of-range weight is neutral, never a stronger preference.
    for bad in (None, "0.5", True, float("nan"), 0.0, 9.0, -1.0):
        assert attention_weight({"attention_weight": bad}) == 1.0
    assert attention_weight({"attention_weight": 0.5}) == 0.5
    assert attention_weight({}) == 1.0
    # A weight nested under "payload" is not where the pool reads it, and must
    # not be silently honoured from the wrong place either.
    assert attention_weight({"payload": {"attention_weight": 0.5}}) == 1.0

    ids = [f"item-{n}" for n in range(2)]
    plain = measure_pool([event(i) for i in ids], now=now, new_item_ids=set(ids))
    quiet = measure_pool([event(i, weight=0.5) for i in ids], now=now, new_item_ids=set(ids))
    loud = measure_pool([event(i, weight=1.5) for i in ids], now=now, new_item_ids=set(ids))

    assert quiet.pool_mass < plain.pool_mass < loud.pool_mass
    # The plain comparison is retained so a weighted decision stays auditable.
    assert quiet.unweighted_pool_mass == plain.unweighted_pool_mass == pytest.approx(plain.pool_mass)
    assert quiet.below_floor == plain.below_floor == loud.below_floor == 0
    assert plain.threshold == WAKE_POOL_THRESHOLD
    # Ratings genuinely change the contact decision, in both directions.
    assert plain.should_wake and not quiet.should_wake
    assert loud.should_wake

    # A requested review wakes regardless of how quiet the source was rated.
    requested = measure_pool([event("asked", weight=0.5, requested=True)],
                             now=now, new_item_ids={"asked"})
    assert requested.should_wake and requested.requested_reviews == 1


def test_feedback_replay_reports_weights_flips_and_holdout(tmp_path):
    """The offline replay must show what would change, and keep a holdout aside.

    It reads a real owner_records store, so the key layout it depends on is
    covered rather than assumed, and it must stay read-only.
    """
    import sqlite3

    from eval.feedback_replay import replay

    store = tmp_path / "sessions.db"
    with sqlite3.connect(store) as connection:
        _ = connection.execute("create table owner_records (owner text, key text, version int, value text)")
        rows = []
        # One watch rated mostly irrelevant, one barely rated at all.
        for index in range(8):
            rows.append(("followups", f"feedback:followups:w_{'a' * 32}:item{index}:rev",
                         1, json.dumps({"value": "irrelevant" if index else "useful"})))
        for index in range(2):
            rows.append(("followups", f"feedback:followups:w_{'b' * 32}:item{index}:rev",
                         1, json.dumps({"value": "useful"})))
        # Two Content items that together clear the threshold unweighted.
        for index in range(2):
            rows.append(("eventmail", f"item:{index}", 1, json.dumps({
                "item_id": f"item-{index}",
                "payload": {"_wake_initial_score": 1.0, "source_name": "s",
                            "attention_weight": 0.5},
            })))
        _ = connection.executemany("insert into owner_records values (?, ?, ?, ?)", rows)

    before = store.read_bytes()
    report = replay(store, holdout=0.0)
    assert store.read_bytes() == before, "replay must not modify the store"

    assert report["ratings"]["total"] == 10
    assert report["ratings"]["watches_rated"] == 2
    # Only the watch with enough ratings is adjusted; the other stays neutral.
    assert report["ratings"]["watches_adjusted"] == 1
    heavy = report["weights"]["w_" + "a" * 32]
    light = report["weights"]["w_" + "b" * 32]
    assert heavy["adjusted"] and heavy["weight"] < 1.0 and heavy["rated"] == 8
    assert not light["adjusted"] and light["weight"] == 1.0

    # The replay shows the contact decision it would have changed.
    decision = report["decision"]
    assert decision["events_replayed"] == 2
    assert decision["would_wake_unweighted"] and not decision["would_wake_weighted"]
    assert decision["decision_flipped"] and decision["direction"] == "quieter"
    assert decision["pool_mass_weighted"] < decision["pool_mass_unweighted"]

    # A holdout removes ratings from the weight and reports them separately.
    withheld = replay(store, holdout=0.5)
    assert withheld["ratings"]["held_out"] > 0
    assert withheld["ratings"]["applied"] + withheld["ratings"]["held_out"] == 10
    # The same split is reproduced on a second run.
    assert replay(store, holdout=0.5)["ratings"]["applied"] == withheld["ratings"]["applied"]
