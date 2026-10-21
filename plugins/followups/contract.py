"""Owner-configured watches; original messages remain in the Ledger."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.plugin_composition import ServiceKey


class WatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    watch_id: str = Field(pattern=r"^w_[0-9a-f]{32}$")
    session_id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=1000)
    url: str = Field(min_length=1, max_length=2048)
    anchors: tuple[str, ...] = Field(min_length=1, max_length=6)
    interval_seconds: int = Field(default=900, ge=60, le=86400)
    review_mode: Literal["each_update", "interest"] = "each_update"
    source_kind: Literal["feed", "webpage"] = "feed"
    xpath: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def region(self):
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


class Followups(Protocol):
    async def overview(self, session_id: str) -> dict: ...
    async def create(self, spec: WatchSpec) -> dict: ...
    async def poll(self, watch_id: str) -> dict: ...
    async def set_enabled(self, request: SetEnabled) -> dict: ...
    async def timeline(self, watch_id: str) -> dict: ...
    async def feedback(self, request: Feedback) -> dict: ...


FOLLOWUPS = ServiceKey[Followups]("followups.v1")
