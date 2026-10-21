"""Real TLS, UID backlog, cancellation and atomic owner-rule updates."""

import asyncio
from types import SimpleNamespace

import pytest

from agent.plugin_composition import CompositionRoot, CredentialRef
from eval.fixtures.mailbox import MailboxServer
from plugins.content.plugin import check_text
from plugins.eventmail.plugin import _SourceServices, _StoreIO
from plugins.eventmail.store import EventMailStore
from plugins.followups.contract import (
    MailboxConfig,
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
    finally:
        await root.dispose()
        log.close()
