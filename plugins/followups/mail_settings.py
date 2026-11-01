"""Mailbox configuration uses native fixed inputs and credential leases."""

import asyncio
import math
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from agent.plugin_composition import CREDENTIALS, ServiceKey
from agent.plugin_composition.config_input import save_credential
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG

from .contract import Config, MailboxConfig, WatchSpec
from .mailbox import MailboxError, collect, watch_idle

MAIL_SETTINGS = ServiceKey("followups.mail-settings.v1")
FIELDS = ("host", "port", "username", "auth_method", "tls_ca_file")
MAX_IDLE_FOLDERS = 8


class Mailboxes:
    def __init__(self, ctx, config):
        self.ctx, self.config = ctx, config
        self._groups = {}
        self._states = {}
        self._running = False

    @staticmethod
    def _folder(spec):
        return "INBOX" if spec.mail_folder.casefold() == "inbox" else spec.mail_folder

    def status(self, spec):
        """Presentation only: no connection, credential access, or user address."""
        if not self.config.enabled or spec.mailbox_id != self.config.account_id:
            return {"mode": "inactive", "reason": "account_not_connected", "retry_seconds": None}
        folder = self._folder(spec)
        if not self._running or spec.watch_id not in self._groups.get(folder, ()):
            return {"mode": "inactive", "reason": "watch_not_active", "retry_seconds": None}
        value = dict(self._states.get(folder, {"mode": "connecting", "reason": "connecting"}))
        retry_at = value.pop("retry_at", None)
        value["retry_seconds"] = None if retry_at is None else max(0, math.ceil(retry_at - time.monotonic()))
        return value

    async def _listen(self, folder, executor, notify):
        failures = 0
        while True:
            started = time.monotonic()
            self._states[folder] = {"mode": "connecting", "reason": "connecting"}
            try:
                # asyncio child tasks do not inherit an OwnerCall. Keep a real
                # permit for this connection until its worker and lease drain.
                async with self.ctx.runtime_scope(), self.ctx.require(CREDENTIALS).open(
                    self.ctx, {"password": self.config.password}
                ) as credentials:
                    supported = await watch_idle(
                        self.config, credentials.credential(self.config.password), folder,
                        executor, notify,
                        lambda: self._states.update({folder: {"mode": "idle", "reason": "server_idle"}}),
                    )
                if not supported:
                    self._states[folder] = {"mode": "polling", "reason": "server_has_no_idle"}
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - optional listener failure cannot disable UID polling
                if time.monotonic() - started >= 60:
                    failures = 0
                delay = min(60, 2 ** min(failures, 6))
                failures += 1
                self._states[folder] = {
                    "mode": "retrying", "reason": "connection_unavailable",
                    "retry_at": time.monotonic() + delay,
                }
                await asyncio.sleep(delay)

    async def follow(self, service):
        """Share bounded folder listeners; signals request the existing UID poll."""
        workers = {}
        executor = ThreadPoolExecutor(max_workers=MAX_IDLE_FOLDERS, thread_name_prefix="mailbox-idle")
        self._running = True

        async def stop(tasks):
            for task in tasks:
                task.cancel()
            cleanup = asyncio.gather(*tasks, return_exceptions=True)
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError

        try:
            async with service.subscribe_changes() as changed:
                while True:
                    changed.clear()
                    watches = await service.enabled_mail_watches()
                    groups = {}
                    for spec in watches:
                        if (self.config.enabled and self.config.password is not None
                            and spec.mailbox_id == self.config.account_id):
                            groups.setdefault(self._folder(spec), []).append(spec.watch_id)
                    self._groups = {folder: tuple(ids) for folder, ids in groups.items()}
                    # Retain live connections first so adding a folder does not
                    # repeatedly evict another one. Extra folders keep polling.
                    selected = [folder for folder in workers if folder in groups]
                    selected.extend(folder for folder in sorted(groups) if folder not in workers)
                    selected = set(selected[:MAX_IDLE_FOLDERS])
                    retired = [workers.pop(folder) for folder in tuple(workers) if folder not in selected]
                    await stop(retired)
                    for folder in tuple(self._states):
                        if folder not in groups:
                            self._states.pop(folder, None)
                    for folder in groups:
                        if folder not in selected:
                            self._states[folder] = {"mode": "polling", "reason": "idle_folder_limit"}
                            continue
                        if folder not in workers:
                            def notify(folder=folder):
                                for watch_id in self._groups.get(folder, ()):
                                    service.request_check(watch_id)
                            workers[folder] = asyncio.create_task(self._listen(folder, executor, notify))
                    await changed.wait()
        finally:
            self._running = False
            self._groups = {}
            try:
                await stop(tuple(workers.values()))
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
                self._states.clear()

    def available(self):
        cfg = self.config
        return (
            [{"account_id": cfg.account_id, "label": cfg.username + " · " + cfg.host}]
            if cfg.enabled
            else []
        )

    async def read(self, spec, cursor):
        cfg = self.config
        if not cfg.enabled or cfg.account_id != spec.mailbox_id or cfg.password is None:
            raise MailboxError(
                "mailbox_unavailable", "原邮箱账号未连接或已更换，请检查邮件账户设置"
            )
        async with self.ctx.require(CREDENTIALS).open(
            self.ctx, {"password": cfg.password}
        ) as credentials:
            return await collect(
                cfg, credentials.credential(cfg.password), spec, cursor
            )


class Settings:
    def __init__(self, ctx):
        self.ctx = ctx

    async def read(self):
        cfg = Config.model_validate(self.ctx.config).mailbox
        return {
            **self.ctx.require(PLUGIN_CONFIG).read(self.ctx),
            "enabled": cfg.enabled,
            "ready": cfg.enabled,
            "blocked": False,
            "can_enable": True,
            "reason": "",
            "has_token": cfg.password is not None,
            "values": {name: getattr(cfg, name) for name in FIELDS},
        }

    async def save(self, request_id, expected_input, values):
        if (
            set(values) - {*FIELDS, "enabled", "password"}
            or type(values.get("enabled")) is not bool
        ):
            raise ValueError("邮箱配置字段无效")
        password = values.get("password")
        if password is not None and (
            not isinstance(password, str)
            or "\x00" in password
            or "\r" in password
            or "\n" in password
        ):
            raise ValueError("邮箱凭据格式无效")
        config = Config.model_validate(self.ctx.config)
        previous = config.mailbox
        data = {
            **previous.model_dump(exclude={"password"}),
            **{k: v for k, v in values.items() if k != "password"},
        }
        data["tls_ca_file"] = data.get("tls_ca_file") or None
        data["host"] = str(data["host"]).strip()
        data["username"] = str(data["username"]).strip()
        changed = (data["host"], data["port"], data["username"]) != (
            previous.host,
            previous.port,
            previous.username,
        )
        data["account_id"] = (
            "ma_" + uuid4().hex
            if changed or previous.account_id is None
            else previous.account_id
        )
        candidate = MailboxConfig.model_validate(
            {
                **data,
                "enabled": False,
                "password": previous.password if not changed else None,
            }
        )
        if values["enabled"]:
            probe = WatchSpec(
                watch_id="w_" + "0" * 32,
                session_id="probe",
                name="连接验证",
                purpose="验证只读连接",
                anchors=("probe",),
                source_kind="mailbox",
                mailbox_id=candidate.account_id,
            )
            if password:
                await collect(candidate, password, probe, {})
            elif candidate.password is not None:
                async with self.ctx.require(CREDENTIALS).open(
                    self.ctx, {"password": candidate.password}
                ) as credentials:
                    await collect(
                        candidate, credentials.credential(candidate.password), probe, {}
                    )
            else:
                raise ValueError("请填写邮箱应用密码或访问令牌")
        credential = (
            save_credential(self.ctx.data_root, password)
            if password
            else candidate.password
        )
        checked = MailboxConfig.model_validate({**data, "password": credential})
        # Preserve the dataclass reference; model_dump would erase its opaque type.
        mailbox = {**checked.model_dump(exclude={"password"}), "password": credential}
        return await self.ctx.require(PLUGIN_CONFIG).apply(
            self.ctx,
            request_id=request_id,
            expected_input=expected_input,
            config={"mailbox": mailbox},
        )

    def receipt(self, request_id):
        return self.ctx.require(PLUGIN_CONFIG).receipt(self.ctx, request_id)
