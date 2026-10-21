"""Owner-configured watches; original messages remain in the Ledger."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.plugin_composition import ServiceKey
from agent.plugin_composition.credentials import CredentialRef
from plugins.ledger.contract import ContentReferences, json_value


class PolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    watch_id: str = Field(pattern=r"^w_[0-9a-f]{32}$")
    revision: int = Field(ge=1)


def check_policy_record(part):
    PolicyRecord.model_validate(json_value(part.value))
    return ContentReferences()


class MailboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    account_id: str | None = Field(default=None, pattern=r"^ma_[0-9a-f]{32}$")
    host: str = Field(default="", max_length=253)
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(default="", max_length=320)
    password: CredentialRef | None = None
    auth_method: Literal["password", "oauth2"] = "password"
    tls_ca_file: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def connection(self):
        if any(c in self.host for c in "/@\\\r\n\x00") or any(
            c in self.username for c in "\r\n\x00"
        ):
            raise ValueError("邮箱服务器或账号无效")
        if self.enabled and not all(
            (self.account_id, self.host.strip(), self.username.strip(), self.password)
        ):
            raise ValueError("开启邮箱需要服务器、账号和独立凭据")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mailbox: MailboxConfig = Field(default_factory=MailboxConfig)


class WatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    watch_id: str = Field(pattern=r"^w_[0-9a-f]{32}$")
    session_id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=1000)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    anchors: tuple[str, ...] = Field(min_length=1, max_length=6)
    interval_seconds: int = Field(default=900, ge=60, le=86400)
    review_mode: Literal["each_update", "interest"] = "each_update"
    source_kind: Literal["feed", "webpage", "mailbox"] = "feed"
    xpath: str | None = Field(default=None, min_length=1, max_length=512)
    mailbox_id: str | None = Field(default=None, pattern=r"^ma_[0-9a-f]{32}$")
    mail_folder: str = Field(default="INBOX", min_length=1, max_length=256)
    mail_sender: str | None = Field(default=None, min_length=1, max_length=320)
    mail_subject: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def region(self):
        if self.source_kind == "mailbox":
            if self.mailbox_id is None or self.url is not None:
                raise ValueError("邮箱跟进需要已连接的账号，不接受信息源 URL")
        elif self.url is None or self.mailbox_id is not None:
            raise ValueError("订阅源和网页需要 URL，不能带邮箱账号")
        if any(c in self.mail_folder + (self.mail_sender or "") for c in "\r\n\x00"):
            raise ValueError("邮箱文件夹或发件人无效")
        if self.xpath is not None:
            if self.source_kind != "webpage":
                raise ValueError("仅网页跟进可设置关注区域")
            from lxml import etree

            try:
                etree.XPath(self.xpath)
            except etree.XPathSyntaxError as error:
                raise ValueError("关注区域 XPath 无效") from error
        return self

    @field_validator("name", "purpose")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("名称和关注内容不能为空")
        return value.strip()

    @field_validator("anchors")
    @classmethod
    def unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("原始对话引用不能重复")
        return value


class WatchId(BaseModel):
    model_config = ConfigDict(extra="forbid")
    watch_id: str = Field(pattern=r"^w_[0-9a-f]{32}$")


class SetEnabled(WatchId):
    enabled: bool


class Feedback(WatchId):
    item_id: str = Field(min_length=1, max_length=256)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    value: Literal["useful", "irrelevant"]


class Revise(WatchId):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    purpose: str = Field(min_length=1, max_length=1000)

    @field_validator("purpose")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("关注规则不能为空")
        return value.strip()


class Recheck(WatchId):
    item_id: str = Field(min_length=1, max_length=256)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class Followups(Protocol):
    async def overview(self, session_id: str) -> dict: ...
    async def create(self, spec: WatchSpec) -> dict: ...
    async def poll(self, watch_id: str) -> dict: ...
    async def set_enabled(self, request: SetEnabled) -> dict: ...
    async def timeline(self, watch_id: str) -> dict: ...
    async def feedback(self, request: Feedback) -> dict: ...
    async def revise(self, request: Revise) -> dict: ...
    async def reset_mailbox(self, watch_id: str) -> dict: ...
    async def recheck(self, request: Recheck) -> dict: ...


FOLLOWUPS = ServiceKey[Followups]("followups.v1")
