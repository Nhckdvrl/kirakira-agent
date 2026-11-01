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
    SetEnabled,
    WatchSpec,
    check_policy_record,
)
from plugins.followups.mail_settings import Mailboxes
from plugins.followups.mailbox import MailboxError, collect, mailbox_name
from plugins.followups.service import Service
from plugins.ledger.contract import ContentPart, Input, SessionAttributes, json_value
from plugins.ledger.log import MessageLog


class _IdleWatchService:
    def __init__(self, specs):
        self.specs = specs
        self.changed = asyncio.Event()
        self.checks = set()
        self.requested = asyncio.Event()

    async def enabled_mail_watches(self):
        return tuple(self.specs)

    @asynccontextmanager
    async def subscribe_changes(self):
        self.changed.set()
        yield self.changed

    def request_check(self, watch_id):
        self.checks.add(watch_id)
        self.requested.set()

    async def take(self, expected):
        async with asyncio.timeout(5):
            while not expected <= self.checks:
                self.requested.clear()
                await self.requested.wait()
        value, self.checks = self.checks, set()
        return value


class _MailboxLeases:
    def __init__(self, secret):
        self.secret, self.active = secret, 0

    @asynccontextmanager
    async def open(self, _ctx, _refs):
        self.active += 1
        try:
            yield SimpleNamespace(credential=lambda _ref: self.secret)
        finally:
            self.active -= 1


async def _until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def _fixture_scope():
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["password", "oauth2"])
async def test_idle_shares_folder_connection_recovers_gap_and_drains_credentials(tmp_path, auth):
    async with MailboxServer(tmp_path / "tls", idle=True) as server:
        server.deliver("基线", "原有邮件")
        server.create_folder("Junk")
        cfg = MailboxConfig.model_validate({**server.config(), "auth_method": auth,
            "password": CredentialRef(("test", "version"))})
        first = WatchSpec(watch_id="w_" + "1" * 32, session_id="owner", name="收件箱",
            purpose="只提醒重要邮件", anchors=("original",), source_kind="mailbox",
            mailbox_id=cfg.account_id, interval_seconds=3600)
        second = first.model_copy(update={"watch_id": "w_" + "2" * 32, "mail_folder": "inbox"})
        junk = first.model_copy(update={"watch_id": "w_" + "3" * 32, "mail_folder": "Junk"})
        baseline = await collect(cfg, server.password, first, {})
        leases = _MailboxLeases(server.password)
        mailboxes = Mailboxes(SimpleNamespace(require=lambda _key: leases, runtime_scope=_fixture_scope), cfg)
        service = _IdleWatchService([first, second, junk])
        server.idle_hold.clear()
        watcher = asyncio.create_task(mailboxes.follow(service))
        try:
            # The server accepts IDLE only after this arrival, and emits no
            # EXISTS for it. The initial wake must close this connection race.
            await asyncio.wait_for(server.idle_entered.wait(), 5)
            race_uid, _ = server.deliver("接入竞态邮件", "及时补查，不重新建基线")
            server.idle_hold.set()
            await server.wait_idle("INBOX")
            await server.wait_idle("Junk")
            assert await service.take({first.watch_id, second.watch_id, junk.watch_id}) == {
                first.watch_id, second.watch_id, junk.watch_id}
            await _until(lambda: mailboxes.status(first)["mode"] == "idle")
            assert leases.active == 2 and len(server.idle_connections) == 2
            assert sum(folder == "INBOX" for folder in server.idle_connections.values()) == 1
            assert not any(row["command"] == "UID FETCH" for row in server.commands)
            initial = await collect(cfg, server.password, first,
                {"initialized": True, "mailbox": baseline.cursor})
            assert initial.entries[0].payload["mail"]["uids"] == [race_uid]
            before = len(server.commands)
            server.notify_idle("INBOX", kind="FETCH")
            await asyncio.sleep(0.1)
            assert not service.checks and not any(row["command"] == "UID FETCH" for row in server.commands[before:])
            # EXISTS/RECENT are wake signals only, not independently admitted
            # mails or model calls. Actual facts still require UID collection.
            server.deliver("新的待办", "请核对截止")
            server.notify_idle("INBOX", kind="RECENT")
            assert await service.take({first.watch_id, second.watch_id}) == {first.watch_id, second.watch_id}
            assert not any(row["command"] == "UID FETCH" for row in server.commands[before:])
            await server.drop_idle("INBOX")
            await _until(lambda: mailboxes.status(first)["mode"] == "retrying")
            assert 0 <= mailboxes.status(first)["retry_seconds"] <= 1
            gap_uid, _ = server.deliver("断线期间的新信", "重连后按UID补查")
            await server.wait_idle("INBOX")
            assert await service.take({first.watch_id, second.watch_id}) == {first.watch_id, second.watch_id}
            recovered = await collect(cfg, server.password, first,
                {"initialized": True, "mailbox": initial.cursor})
            assert gap_uid in [uid for entry in recovered.entries for uid in entry.payload["mail"]["uids"]]
            await server.drop_idle("INBOX")
            await _until(lambda: mailboxes.status(first)["mode"] == "retrying")
            assert 1 <= mailboxes.status(first)["retry_seconds"] <= 2
            await server.wait_idle("INBOX")
            await service.take({first.watch_id, second.watch_id})
            # Pausing one watch retains the shared connection for the other.
            service.specs = [second, junk]
            service.changed.set()
            await _until(lambda: mailboxes.status(first)["mode"] == "inactive")
            assert leases.active == 2
            service.checks.clear()
            server.notify_idle("INBOX")
            assert await service.take({second.watch_id}) == {second.watch_id}
            service.specs = [junk]
            service.changed.set()
            await _until(lambda: leases.active == 1 and "INBOX" not in server.idle_connections.values())
            assert mailboxes.status(second)["mode"] == "inactive"
            # Cancellation also drains a worker waiting for the server's IDLE
            # continuation, before releasing the credential it was using.
            server.idle_entered.clear()
            server.idle_hold.clear()
            service.specs = [first, junk]
            service.changed.set()
            await asyncio.wait_for(server.idle_entered.wait(), 5)
            assert leases.active == 2 and mailboxes.status(first)["mode"] == "connecting"
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(watcher, 5)
            server.idle_hold.set()
        assert leases.active == 0
        await _until(lambda: not server.connections and not server.handlers)
        allowed = {"CAPABILITY", "LOGIN", "AUTHENTICATE", "EXAMINE", "IDLE", "DONE", "UID SEARCH", "UID FETCH"}
        assert all(row["command"] in allowed for row in server.commands)
        assert all(row["peek"] for row in server.commands if row["command"] == "UID FETCH")


@pytest.mark.asyncio
async def test_idle_unavailable_and_excess_folders_keep_explicit_polling_status(tmp_path, monkeypatch):
    import plugins.followups.mail_settings as settings

    monkeypatch.setattr(settings, "MAX_IDLE_FOLDERS", 1)
    for supported in (False, True):
        async with MailboxServer(tmp_path / str(supported), idle=supported) as server:
            server.create_folder("Junk")
            cfg = MailboxConfig.model_validate({**server.config(), "password": CredentialRef(("test", "version"))})
            inbox = WatchSpec(watch_id="w_" + "4" * 32, session_id="owner", name="邮箱",
                purpose="检查新邮件", anchors=("original",), source_kind="mailbox", mailbox_id=cfg.account_id)
            junk = inbox.model_copy(update={"watch_id": "w_" + "5" * 32, "mail_folder": "Junk"})
            service, leases = _IdleWatchService([inbox, junk]), _MailboxLeases(server.password)
            mailboxes = Mailboxes(SimpleNamespace(require=lambda _key, leases=leases: leases, runtime_scope=_fixture_scope), cfg)
            watcher = asyncio.create_task(mailboxes.follow(service))
            try:
                await _until(lambda mailboxes=mailboxes, inbox=inbox, supported=supported:
                    mailboxes.status(inbox)["mode"] == ("idle" if supported else "polling"))
                assert mailboxes.status(junk) == {"mode": "polling", "reason": "idle_folder_limit", "retry_seconds": None}
                assert leases.active == int(supported)
                if not supported:
                    assert mailboxes.status(inbox)["reason"] == "server_has_no_idle"
                    assert not service.checks and not any(row["command"] == "IDLE" for row in server.commands)
                baseline = await mailboxes.read(inbox, {})
                new_uid, _ = server.deliver("新信", "原有轮询仍可收集")
                batch = await mailboxes.read(inbox, {"initialized": True, "mailbox": baseline.cursor})
                assert batch.entries[0].payload["mail"]["uids"] == [new_uid]
                # Presenting the mode never resolves credentials or returns an address.
                assert server.username not in json.dumps(mailboxes.status(inbox))
                assert server.password not in json.dumps(mailboxes.status(inbox))
            finally:
                watcher.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(watcher, 5)
            assert leases.active == 0
            await _until(lambda: not server.connections and not server.handlers)


@pytest.mark.asyncio
async def test_native_runtime_idle_and_automatic_uid_poll_hold_real_credential_authority(tmp_path, monkeypatch):
    import sqlite3
    import threading

    from agent.plugin_composition.config_input import save_config, save_credential
    from agent.plugin_composition.credentials import CredentialClients
    from core.common.file_io import run_file_io
    from kirakira_agent.runtime import initialize_workspace, rpc, runtime
    from plugins.models.contract import MODEL_CATALOG

    monkeypatch.setenv("AKASHIC_PLUGIN_HOME", str(tmp_path / "plugin-home"))
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    original_create = CredentialClients.create
    admitted, denied = [], []

    async def checked_create(self, ctx, refs):
        try:
            client = await original_create(self, ctx, refs)
        except Exception as error:
            denied.append((type(error).__name__, getattr(error, "code", None)))
            raise
        admitted.append(ctx.runtime.plugin_id)
        return client

    monkeypatch.setattr(CredentialClients, "create", checked_create)
    session = "akashic:idle-authority-fixture"
    log = MessageLog(workspace / "sessions.db")
    log.ensure_session(session, SessionAttributes())
    log.writer(session, author="user", source="conversation", body_types=(Input,),
               content={"text": check_text}).append("original", Input((ContentPart("text", "跟进新邮件"),)))
    from plugins.ledger.contract import Output
    log.writer(session, author="agent", source="conversation", body_types=(Output,), content={}).append(
        "recorded", Output((), "complete"))
    log.close()

    async def observed(watch_id):
        def read():
            with sqlite3.connect((workspace / "sessions.db").as_uri() + "?mode=ro", uri=True) as connection:
                row = connection.execute("SELECT value FROM owner_records WHERE key=?", ("cursor:" + watch_id,)).fetchone()
            event_path = workspace / "plugin-data/eventmail-builtin/eventmail.sqlite3"
            with sqlite3.connect(event_path.as_uri() + "?mode=ro", uri=True) as connection:
                count = connection.execute("SELECT COUNT(*) FROM items WHERE source_id=?", ("followups:" + watch_id,)).fetchone()[0]
            return (json.loads(row[0]) if row else {}), count
        return await run_file_io(read)

    async with MailboxServer(tmp_path / "tls", idle=True) as server:
        server.deliver("已有邮件", "首次基线不补发")
        data = workspace / "plugin-data/followups-builtin"
        password = save_credential(data, server.password)
        save_config(data, {"mailbox": {**server.config(), "password": password}})
        async with runtime(workspace) as core:
            assert not core.plugin_manager.live_root.context.require(MODEL_CATALOG).snapshot().models
            spec = WatchSpec(watch_id="w_" + "6" * 32, session_id=session, name="邮箱",
                purpose="跟进新邮件", anchors=("original",), source_kind="mailbox",
                mailbox_id=server.config()["account_id"], interval_seconds=3600)
            await rpc(core, "followups/create", spec.model_dump(mode="json"))
            # Never call poll or timeline: either would enter the owner on the
            # RPC task and could conceal the missing background-task permit.
            async with asyncio.timeout(15):
                while True:
                    cursor, count = await observed(spec.watch_id)
                    if cursor.get("initialized"):
                        break
                    await asyncio.sleep(0.01)
            await server.wait_idle()
            assert count == 0
            server.deliver("重要的新邮件", "IDLE唤醒原生UID采集，不等待一小时轮询")
            async with asyncio.timeout(15):
                while (await observed(spec.watch_id))[1] != 1:
                    await asyncio.sleep(0.01)
            assert not denied and admitted.count("followups") >= 3
            assert all(row["peek"] for row in server.commands if row["command"] == "UID FETCH")
        await _until(lambda: not server.handlers and not server.connections)
        assert not any(thread.name.startswith("mailbox-idle") for thread in threading.enumerate())


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
                        writers=lambda session: log.writer(
                            session, author="user", source="followups", body_types=(Input,),
                            content={"text": check_text, "followups.policy": check_policy_record}),
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
                frozen_items = json_value(original_intent["items"])
                assert len(frozen_items) == 10
                assert not native.snapshot(datetime.now(UTC))["items"]
                # A timeline is observational: it cannot retry a submission or
                # quietly start proactive work while the owner checks status.
                assert not (await service.timeline(spec.watch_id))["events"]
                await service.set_enabled(SetEnabled(watch_id=spec.watch_id, enabled=False))
                assert (await service.poll(spec.watch_id))["status"] == "paused"
                assert not native.snapshot(datetime.now(UTC))["items"]
                assert service.store.read("intent:" + spec.watch_id).value == original_intent
                await service.revise(Revise(
                    watch_id=spec.watch_id, request_id="new-rule-after-collection",
                    expected_revision=0, purpose="之后只关注临近截止的事项"))
                # Mail may be moved by the owner's client between collection
                # and native admission. Recovery owns the facts already read.
                for uid in range(50, 60):
                    del junk.messages[uid]
                fetches_before_recovery = [row for row in server.commands
                    if row["command"] == "UID FETCH"]

            # Restart must retain the original window even after a new arrival.
            async with opened() as service:
                assert (await service.poll(spec.watch_id))["status"] == "paused"
                assert not native.snapshot(datetime.now(UTC))["items"]
                assert service.store.read("intent:" + spec.watch_id).value == original_intent
                await service.set_enabled(SetEnabled(watch_id=spec.watch_id, enabled=True))
                async def crash(*_):
                    intended = service.store.read("intent:" + spec.watch_id).value
                    assert intended == original_intent
                    raise RuntimeError("crash after native mail commit")

                service._finish = crash
                with pytest.raises(RuntimeError, match="native mail commit"):
                    await service.poll(spec.watch_id)
                assert len(native.snapshot(datetime.now(UTC))["items"]) == 10
                assert fetches_before_recovery == [row for row in server.commands
                    if row["command"] == "UID FETCH"]
                for item in frozen_items:
                    accepted = await (await service._source(spec.watch_id)).inspect_revision(
                        item["item_id"], item["revision"])
                    assert accepted["payload"] == item["payload"]
                    assert accepted["payload"]["follow_up"]["policy_revision"] == 0
                    assert accepted["payload"]["follow_up"]["purpose"] == spec.purpose
                await service.set_enabled(SetEnabled(watch_id=spec.watch_id, enabled=False))

            # Accepted native receipts recover even when those messages vanish.
            async with opened() as service:
                assert (await service.poll(spec.watch_id))["status"] == "paused"
                assert not service.store.read("intent:" + spec.watch_id).value
                assert list(service.store.read("cursor:" + spec.watch_id).value[
                    "mailbox"]["initial_uids"]) == [60, 61]
                assert len(native.snapshot(datetime.now(UTC))["items"]) == 10
                await service.set_enabled(SetEnabled(watch_id=spec.watch_id, enabled=True))
                second = await service.poll(spec.watch_id)
                assert second["scanned"] == 2 and second["pending"] == 1
                assert len(native.snapshot(datetime.now(UTC))["items"]) == 12
                assert (await service.poll(spec.watch_id))["scanned"] == 1
                events = (await service.timeline(spec.watch_id))["events"]
                assert len(events) == 13
                assert all(event["payload"]["mail"]["folder"] == "Junk" for event in events)
                for event in events:
                    if min(event["payload"]["mail"]["uids"]) >= 60:
                        assert event["payload"]["follow_up"]["policy_revision"] == 1
                        assert event["payload"]["follow_up"]["purpose"] == "之后只关注临近截止的事项"
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
