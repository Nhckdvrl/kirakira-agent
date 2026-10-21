"""Mailbox configuration uses native fixed inputs and credential leases."""

from uuid import uuid4

from agent.plugin_composition import CREDENTIALS, ServiceKey
from agent.plugin_composition.config_input import save_credential
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG

from .contract import Config, MailboxConfig, WatchSpec
from .mailbox import MailboxError, collect

MAIL_SETTINGS = ServiceKey("followups.mail-settings.v1")
FIELDS = ("host", "port", "username", "auth_method", "tls_ca_file")


class Mailboxes:
    def __init__(self, ctx, config):
        self.ctx, self.config = ctx, config

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
