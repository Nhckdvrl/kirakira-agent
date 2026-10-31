"""Real TLS, UID backlog, cancellation and atomic owner-rule updates."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from agent.plugin_composition import CompositionRoot, CredentialRef
from eval.fixtures.mailbox import MailboxServer
from plugins.content.plugin import check_text
from plugins.eventmail.contract import REQUESTED_REVIEW
from plugins.eventmail.plugin import _SourceServices, _StoreIO
from plugins.eventmail.store import EventMailStore
from plugins.followups.contract import (
    MailboxConfig,
    Recheck,
    Revise,
    WatchSpec,
    check_policy_record,
)
from plugins.followups.mailbox import MailboxError, collect, mailbox_name
from plugins.followups.service import Service
from plugins.ledger.contract import ContentPart, Input, SessionAttributes
from plugins.ledger.log import MessageLog


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["password", "oauth2"])
async def test_tls_read_only_backlog_threads_duplicate_identity_and_uid_reset(
    tmp_path, auth
):
    async with MailboxServer(tmp_path / "tls") as server:
        server.deliver("青禾 历史邮件", "首次采集以前的邮件")
        cfg = MailboxConfig.model_validate(
            {
                **server.config(),
                "auth_method": auth,
                "password": CredentialRef(("test", "version")),
            }
        )
        spec = WatchSpec(
            watch_id="w_" + "a" * 32,
            session_id="owner",
            name="项目",
            purpose="关注改期",
            anchors=("original",),
            source_kind="mailbox",
            mailbox_id=cfg.account_id,
            mail_sender="updates@example.test",
            mail_subject="青禾",
        )
        baseline = await collect(cfg, server.password, spec, {})
        assert baseline.cursor["last_uid"] == 1 and not baseline.entries
        _, root = server.deliver("青禾 会议安排", "先安排周三。" + "旧说明。" * 2000)
        server.deliver(
            "Re: 青禾 会议安排", "取消周三，已改为周五下午。验证码：873921", thread=root
        )
        for _ in range(13):
            server.deliver("无关广告", "不会进入模型", sender="news@example.test")
        cursor = {"initialized": True, "mailbox": baseline.cursor}
        first = await collect(cfg, server.password, spec, cursor)
        assert first.scanned == 10 and first.matched == 2 and first.pending == 5
        assert (
            len(first.entries) == 1
            and first.entries[0].payload["mail"]["messages_in_update"] == 2
        )
        assert "取消周三" in first.entries[0].payload["content"][:1000]
        assert "873921" not in first.entries[0].payload["content"]
        assert first.entries[0].payload["content_truncated"]
        second = await collect(
            cfg, server.password, spec, {"initialized": True, "mailbox": first.cursor}
        )
        assert second.scanned == 5 and second.pending == 0 and not second.entries
        latest_uid = server.uidnext
        server.messages[latest_uid] = server.messages[3]
        server.uidnext += 1
        duplicate = await collect(
            cfg, server.password, spec, {"initialized": True, "mailbox": second.cursor}
        )
        assert not duplicate.entries and duplicate.duplicates == 1
        server.uidvalidity += 1
        with pytest.raises(MailboxError, match="身份已变化"):
            await collect(
                cfg,
                server.password,
                spec,
                {"initialized": True, "mailbox": duplicate.cursor},
            )
        with pytest.raises(MailboxError, match="TLS"):
            await collect(
                cfg.model_copy(update={"tls_ca_file": None}), server.password, spec, {}
            )
        allowed = {
            "CAPABILITY",
            "LOGIN",
            "AUTHENTICATE",
            "EXAMINE",
            "UID SEARCH",
            "UID FETCH",
        }
        assert all(row["command"] in allowed for row in server.commands)
        assert all(
            row["peek"] for row in server.commands if row["command"] == "UID FETCH"
        )
        assert mailbox_name('收件箱&"') == '"&ZTZO9nux-&-\\""'


@pytest.mark.asyncio
async def test_cancel_drains_actual_imap_work_before_return(tmp_path):
    async with MailboxServer(tmp_path / "tls") as server:
        cfg = MailboxConfig.model_validate(
            {**server.config(), "password": CredentialRef(("test", "version"))}
        )
        spec = WatchSpec(
            watch_id="w_" + "a" * 32,
            session_id="owner",
            name="项目",
            purpose="关注",
            anchors=("original",),
            source_kind="mailbox",
            mailbox_id=cfg.account_id,
        )
        baseline = await collect(cfg, server.password, spec, {})
        server.deliver("New", "Body")
        server.hold.clear()
        work = asyncio.create_task(
            collect(
                cfg,
                server.password,
                spec,
                {"initialized": True, "mailbox": baseline.cursor},
            )
        )
        await asyncio.wait_for(server.fetch_started.wait(), 5)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work, 5)


@pytest.mark.asyncio
async def test_recent_junk_plan_survives_cancel_restart_and_native_commit(tmp_path):
    log = MessageLog(tmp_path / "ledger.db")
    log.ensure_session("owner", SessionAttributes())
    log.writer("owner", author="user", source="conversation", body_types=(Input,),
               content={"text": check_text}).append(
        "original", Input((ContentPart("text", "请检查 Junk 最近12封邮件里的重要事项，然后跟进新邮件。"),)))
    native = EventMailStore(tmp_path / "events.db")
    native.initialize()
    sources = _SourceServices(native, _StoreIO(), lambda: None)

    try:
        async with MailboxServer(tmp_path / "tls") as server:
            server.deliver("另一文件夹的重要信", "不得误读 INBOX")
            old, _ = server.deliver("窗口外重要信", "不得扩大回查", folder="Junk")
            junk = server.folders["Junk"]
            junk.uidnext = 50  # UID gaps must not shrink the actual-message window.
            for index in range(12):
                server.deliver(f"历史信{index}", f"需核对的真实材料{index}", folder="Junk")
            cfg = MailboxConfig.model_validate(
                {**server.config(), "password": CredentialRef(("test", "version"))})
            spec = WatchSpec(
                watch_id="w_" + "c" * 32, session_id="owner", name="Junk检查",
                purpose="检查已有重要事项", anchors=("original",), source_kind="mailbox",
                mailbox_id=cfg.account_id, mail_folder="Junk", mail_initial_limit=12,
                review_mode="interest",
            )

            async def read_mailbox(spec, cursor):
                return await collect(cfg, server.password, spec, cursor)

            @asynccontextmanager
            async def opened():
                root, mounted = CompositionRoot("mail-history-test"), []

                async def mount(ctx):
                    service = Service(
                        ctx, log.owner("followups"), log.catalog(), sources,
                        SimpleNamespace(recent=lambda **_: ()), None, lambda: None,
                        mailbox=SimpleNamespace(
                            available=lambda: [{"account_id": cfg.account_id}], read=read_mailbox),
                    )
                    await ctx.effect(lambda: service.http.aclose)
                    mounted.append(service)

                fiber = await root.mount(mount, name="followups")
                assert fiber.error is None
                try:
                    yield mounted[0]
                finally:
                    await root.dispose()

            # Existing watches remain future-only and never FETCH old bodies.
            baseline = await collect(cfg, server.password,
                                     spec.model_copy(update={"mail_initial_limit": 0}), {})
            assert baseline.cursor["last_uid"] == 61 and not baseline.entries
            assert not any(row["command"] == "UID FETCH" for row in server.commands)
            with pytest.raises(MailboxError, match="只读操作"):
                await collect(cfg, server.password, spec.model_copy(update={"mail_folder": "Spam"}), {})

            async with opened() as service:
                await service.create(spec)
                server.hold.clear()
                work = asyncio.create_task(service.poll(spec.watch_id))
                await asyncio.wait_for(server.fetch_started.wait(), 5)
                admitted = service.store.read("cursor:" + spec.watch_id).value
                assert admitted["initialized"] and admitted["mail_initial_started"]
                assert admitted["mailbox"]["last_uid"] == 61
                assert list(admitted["mailbox"]["initial_uids"]) == list(range(50, 62))
                assert not native.snapshot(datetime.now(UTC))["items"]
                server.deliver("冻结窗口后的新信", "须在回查之后追上", folder="Junk")
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(work, 5)
                assert service.store.read("cursor:" + spec.watch_id).value == admitted
                server.hold.set()

            # An unsubmitted intent also survives restart with its exact review
            # choice and timestamp, rather than reverting to the future mode.
            async with opened() as service:
                source = await service._source(spec.watch_id)

                async def not_submitted(*_):
                    raise RuntimeError("crash before native mail commit")

                source.submit = not_submitted
                with pytest.raises(RuntimeError, match="before native mail commit"):
                    await service.poll(spec.watch_id)
                original_intent = service.store.read("intent:" + spec.watch_id).value
                assert original_intent["review_policy"] == REQUESTED_REVIEW
                assert not native.snapshot(datetime.now(UTC))["items"]

            # Restart must retain the original window even after a new arrival.
            async with opened() as service:
                async def crash(*_):
                    intended = service.store.read("intent:" + spec.watch_id).value
                    assert intended == original_intent
                    raise RuntimeError("crash after native mail commit")

                service._finish = crash
                with pytest.raises(RuntimeError, match="native mail commit"):
                    await service.poll(spec.watch_id)
                assert len(native.snapshot(datetime.now(UTC))["items"]) == 10
                for uid in range(50, 60):
                    del junk.messages[uid]

            # Accepted native receipts recover even when those messages vanish.
            async with opened() as service:
                second = await service.poll(spec.watch_id)
                assert second["scanned"] == 2 and second["pending"] == 1
                assert len(native.snapshot(datetime.now(UTC))["items"]) == 12
                assert (await service.poll(spec.watch_id))["scanned"] == 1
                events = (await service.timeline(spec.watch_id))["events"]
                assert len(events) == 13
                assert all(event["payload"]["mail"]["folder"] == "Junk" for event in events)
                for event in events:
                    if max(event["payload"]["mail"]["uids"]) <= 61:
                        assert event["payload"]["review_policy"] == REQUESTED_REVIEW
                    else:
                        assert "review_policy" not in event["payload"]
                assert service._watch(spec.watch_id)["spec"]["review_mode"] == "interest"
                assert (await service.poll(spec.watch_id))["submitted"] == 0
                assert len((await service.timeline(spec.watch_id))["events"]) == 13
                fetches = [row for row in server.commands if row["command"] == "UID FETCH"]
                assert all(row["folder"] == "Junk" and row["uid"] != old and row["peek"]
                           for row in fetches)
                junk.uidvalidity += 1
                assert (await service.poll(spec.watch_id))["error"] == "uidvalidity_changed"
                await service.reset_mailbox(spec.watch_id)
                assert (await service.poll(spec.watch_id))["status"] == "baseline"
                assert fetches == [row for row in server.commands if row["command"] == "UID FETCH"]
                assert len((await service.timeline(spec.watch_id))["events"]) == 13
    finally:
        log.close()


@pytest.mark.asyncio
async def test_rule_fact_and_policy_commit_atomically_retry_and_conflict(tmp_path):
    log = MessageLog(tmp_path / "ledger.db")
    log.ensure_session("owner", SessionAttributes())

    def writer(source):
        return log.writer(
            "owner",
            author="user",
            source=source,
            body_types=(Input,),
            content={"text": check_text, "followups.policy": check_policy_record},
        )

    writer("conversation").append(
        "original", Input((ContentPart("text", "跟进项目动态"),))
    )
    mail = EventMailStore(tmp_path / "events.db")
    mail.initialize()
    sources = _SourceServices(mail, _StoreIO(), lambda: None)
    root, services = CompositionRoot("policy-test"), []
    feed = {"content": None, "requests": 0}

    def handle(_request):
        feed["requests"] += 1
        item = (
            ""
            if feed["content"] is None
            else "<item><guid>stable</guid><title>项目更新</title><description>"
            + feed["content"]
            + "</description></item>"
        )
        return httpx.Response(
            200, content=("<rss><channel>" + item + "</channel></rss>").encode()
        )

    async def mount(ctx):
        service = Service(
            ctx,
            log.owner("followups"),
            log.catalog(),
            sources,
            SimpleNamespace(recent=lambda **_: ()),
            None,
            lambda: None,
            writers=lambda _: writer("followups"),
        )
        await service.http.aclose()
        service.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        await ctx.effect(lambda: service.http.aclose)
        services.append(service)

    try:
        fiber = await root.mount(mount, name="policy")
        assert fiber.error is None
        service = services[0]
        spec = WatchSpec(
            watch_id="w_" + "b" * 32,
            session_id="owner",
            name="青禾",
            purpose="原规则",
            url="https://example.test/feed",
            anchors=("original",),
        )
        await service.create(spec)
        assert (await service.poll(spec.watch_id))["status"] == "baseline"
        feed["content"] = "第一版来源内容"
        original_event = await service.poll(spec.watch_id)
        ref = original_event["refs"][0]
        request = Revise(
            watch_id=spec.watch_id,
            request_id="once",
            expected_revision=0,
            purpose="以后仅在需要我决定时提醒，取消普通进度通知。",
        )
        original_transact = service.store.transact_async

        async def failed(commit):
            def operation(tx):
                commit(tx)
                raise RuntimeError("crash before transaction commit")

            return await original_transact(operation)

        service.store.transact_async = failed
        with pytest.raises(RuntimeError, match="crash"):
            await service.revise(request)
        assert not any(m.source == "followups" for m in log.reader("owner").snapshot())
        assert service._policy(service._watch(spec.watch_id))["revision"] == 0
        service.store.transact_async = original_transact
        receipt = await service.revise(request)
        assert await service.revise(request) == receipt
        with pytest.raises(ValueError):
            await service.revise(
                request.model_copy(update={"request_id": "other", "purpose": "Stale"})
            )
        rule = log.reader("owner").get(receipt["message_id"])
        assert rule.author == "user" and rule.source == "followups"
        assert rule.body.parts[0].value == request.purpose
        timeline = await service.timeline(spec.watch_id)
        assert timeline["watch"]["policy"]["revision"] == 1
        assert timeline["watch"]["policy"]["purpose"] == request.purpose
        assert len(timeline["policy_history"]) == 1
        assert log.reader("owner").get("original").body.parts[0].value == "跟进项目动态"
        previous_cursor = service.store.read("cursor:" + spec.watch_id).value
        previous_requests = feed["requests"]
        review = await service.recheck(Recheck(watch_id=spec.watch_id, **ref))
        assert review["submitted"] == 1 and feed["requests"] == previous_requests
        assert service.store.read("cursor:" + spec.watch_id).value == previous_cursor
        assert (await service.recheck(Recheck(watch_id=spec.watch_id, **ref)))[
            "status"
        ] == "reused"
        timeline = await service.timeline(spec.watch_id)
        assert len(timeline["events"]) == 2
        by_revision = {row["revision"]: row["payload"] for row in timeline["events"]}
        assert by_revision[ref["revision"]]["follow_up"]["purpose"] == "原规则"
        newer = by_revision[review["refs"][0]["revision"]]
        assert (
            newer["content"] == "第一版来源内容"
            and newer["follow_up"]["purpose"] == request.purpose
        )
        from plugins.wake.messages import recent_context

        def captured_policy(payload=newer, source="followups:" + spec.watch_id):
            return recent_context(
                log.catalog(),
                SimpleNamespace(recent=lambda **_: ()),
                target="owner",
                now=datetime.now(UTC),
                items=({"ref": {"source_id": source}, "payload": payload},),
            )

        raw_rules = json.loads(captured_policy())["followup_rule_changes"]
        assert len(raw_rules) == 1 and raw_rules[0]["text"] == request.purpose
        assert raw_rules[0]["message_id"] == receipt["message_id"]
        assert not json.loads(captured_policy(source="external-mail"))[
            "followup_rule_changes"
        ]
        with pytest.raises(ValueError, match="来源、版本"):
            captured_policy(source="followups:w_" + "c" * 32)
        with pytest.raises(ValueError, match="用户文字"):
            captured_policy(
                {**newer, "follow_up": {**newer["follow_up"], "purpose": "forged"}}
            )
        assert newer["owner_recheck"]["original_revision"] == ref["revision"]
        feed["content"] = "第二版来源内容"
        await service.poll(spec.watch_id)
        with pytest.raises(ValueError, match="较新版本"):
            await service.recheck(Recheck(watch_id=spec.watch_id, **ref))
    finally:
        await root.dispose()
        log.close()
