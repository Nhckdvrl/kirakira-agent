"""Conversation tools bind watches to real inputs and durable call receipts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from plugins.ledger.contract import ContentPart, Input, Output, ToolCall, json_value
from plugins.tools.contract import Result, TOOLS

from .contract import Revise, SetEnabled, WatchSpec
from .feeds import fingerprint
from .service import text


class Read(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    watch_id: str | None = Field(default=None, pattern=r"^w_[0-9a-f]{32}$")
    offset: int = Field(default=0, ge=0, le=100)
    limit: int = Field(default=5, ge=1, le=10)


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(
        min_length=1,
        max_length=1000,
        description="逐字摘录本轮用户明确要求持续关注的原话，包含通知条件；不能改写或补充条件。",
    )
    source_kind: Literal["feed", "webpage", "mailbox"]
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    interval_seconds: int = Field(default=900, ge=60, le=86400)
    review_mode: Literal["each_update", "interest"] = "each_update"
    xpath: str | None = Field(default=None, min_length=1, max_length=512)
    mailbox_id: str | None = Field(default=None, pattern=r"^ma_[0-9a-f]{32}$")
    mail_folder: str = Field(default="INBOX", min_length=1, max_length=256)
    mail_sender: str | None = Field(default=None, min_length=1, max_length=320)
    mail_subject: str | None = Field(default=None, min_length=1, max_length=200)


class Manage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    watch_id: str = Field(pattern=r"^w_[0-9a-f]{32}$")
    action: Literal["pause", "resume", "revise"]
    purpose: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="revise 必填：逐字摘录本轮用户的新关注规则，其他动作不填写。",
    )

    @model_validator(mode="after")
    def rule(self):
        if (self.action == "revise") != (self.purpose is not None):
            raise ValueError("仅 revise 必须填写 purpose，暂停或恢复不能附带修改规则")
        return self


def origin(source, *, write):
    if source is None:
        raise ValueError("跟进工具需要实际对话中的调用")
    calling = next(
        (
            row
            for row in source.messages
            if row.message_id == source.call_ref.message_id
        ),
        None,
    )
    if (
        calling is None
        or not isinstance(calling.body, Output)
        or not 0 <= source.call_ref.part_index < len(calling.body.parts)
        or not isinstance(calling.body.parts[source.call_ref.part_index], ToolCall)
    ):
        raise ValueError("跟进工具缺少原始调用")
    if not write:
        return calling.session_id, None
    if calling.source != "conversation":
        raise ValueError("创建或修改跟进需要用户当前对话；后台任务不能自行扩大持续关注")
    previous = [
        row
        for row in source.messages
        if row.session_id == calling.session_id
        and row.source == calling.source
        and row.seq < calling.seq
    ]
    inputs = [
        row for row in previous if isinstance(row.body, Input) and row.author == "user"
    ]
    if not inputs or any(
        isinstance(row.body, Output)
        and row.body.finish in {"complete", "quiet"}
        and row.seq > inputs[-1].seq
        for row in previous
    ):
        raise ValueError("跟进操作需要本轮原始用户输入")
    return calling.session_id, inputs[-1]


def watch_summary(watch):
    spec, policy = watch["spec"], watch.get("policy", {})
    return {
        "watch_id": spec["watch_id"],
        "name": spec["name"],
        "enabled": watch["enabled"],
        "source_kind": spec["source_kind"],
        "url": spec.get("url"),
        "interval_seconds": spec["interval_seconds"],
        "review_mode": spec["review_mode"],
        "purpose": policy.get("purpose", spec["purpose"]),
        "policy_revision": policy.get("revision", 0),
        "origin_message_id": policy.get("origin_message_id")
        or next(iter(spec.get("anchors", ())), None),
    }


def result(value, outcome="success"):
    return Result(
        outcome, (ContentPart("text", json.dumps(value, ensure_ascii=False)),)
    )


def receipt(record):
    value = record["result"]
    if record["action"] == "revise":
        return result(
            {
                "status": "rule_updated",
                **value,
                "note": "新规则用于之后的新变化；已接纳的调查可能按旧规则完成。",
            }
        )
    return result(
        {
            "status": "created" if record["action"] == "create" else "enabled_changed",
            "watch": watch_summary(value),
            "note": "已保存。首次成功读取只建基线；创建成功不等于已读取来源或已经发出通知。通知位置沿用主动联系设置，不自动改到当前会话。"
            if record["action"] == "create"
            else "暂停只停止后续采集，已经接纳的调查可能继续完成。",
        }
    )


class FollowupTool:
    idempotent = True

    def __init__(self, service, action, readiness=None):
        self.service, self.action, self.readiness = service, action, readiness

    async def _contact(self, session):
        if self.readiness is None:
            return {"ready": False, "reason": "主动联系状态不可用"}
        state = await self.readiness()
        delivery = state.get("values", {}).get("delivery") or {}
        return {
            "enabled": state["enabled"] is True,
            "ready": state["ready"] is True,
            "reason": state["reason"] or ("" if state["enabled"] else "主动联系未开启"),
            "delivery_channel": delivery.get("channel"),
            "delivery_session_id": delivery.get("session_id"),
            "delivery_to_current_conversation": delivery.get("session_id") == session,
        }

    async def prepare(self, arguments, source=None):
        try:
            schema = {"read": Read, "create": Create, "manage": Manage}[self.action]
            request = schema.model_validate(json_value(arguments))
            session, user = origin(source, write=self.action != "read")
            overview = await self.service.overview(session)
            watches = {row["spec"]["watch_id"]: row for row in overview["watches"]}
            if self.action == "read":
                if request.watch_id is not None and request.watch_id not in watches:
                    raise ValueError("只能查看当前对话的跟进，请在创建它的对话操作")
                return {"session_id": session, **request.model_dump()}
            if self.action == "create" or request.action == "resume":
                contact = await self._contact(session)
                if not contact["ready"]:
                    raise ValueError(
                        "尚不能启动跟进通知："
                        + contact["reason"]
                        + "。请在主动联系设置中检查记忆、聊天模型和通知位置并开启；本次没有创建或恢复跟进。"
                    )
            if request.purpose is not None and request.purpose.strip() not in text(
                user
            ):
                raise ValueError(
                    "purpose 必须逐字引用本轮用户原话，不能用模型概括代替用户规则"
                )
            if self.action == "create":
                spec = WatchSpec(
                    watch_id="w_" + fingerprint(source.effect_key)[:32],
                    session_id=session,
                    anchors=(user.message_id,),
                    **request.model_dump(),
                )
                return {"spec": spec.model_dump(mode="json")}
            watch = watches.get(request.watch_id)
            if watch is None:
                raise ValueError(
                    "只能修改当前对话的跟进；先 read_followups 获取准确标识"
                )
            if request.action == "revise":
                revision = Revise(
                    watch_id=request.watch_id,
                    request_id=fingerprint(source.effect_key),
                    expected_revision=watch["policy"]["revision"],
                    purpose=request.purpose,
                    origin_message_id=user.message_id,
                )
                return {"revision": revision.model_dump(mode="json")}
            return {
                "session_id": session,
                "enabled": SetEnabled(
                    watch_id=request.watch_id, enabled=request.action == "resume"
                ).model_dump(mode="json"),
            }
        except ValueError as error:
            return str(error)

    async def invoke(self, key, arguments):
        try:
            if self.action == "read":
                return result(await self._read(arguments))
            if self.action == "create":
                if await self.service.operation(key) is None:
                    contact = await self._contact(arguments["spec"]["session_id"])
                    if not contact["ready"]:
                        raise ValueError(
                            "主动联系在执行前已不可用，本次没有新建跟进："
                            + contact["reason"]
                        )
                await self.service.create(
                    WatchSpec.model_validate(json_value(arguments["spec"])),
                    operation_key=key,
                )
            elif "revision" in arguments:
                await self.service.revise(
                    Revise.model_validate(json_value(arguments["revision"])),
                    operation_key=key,
                )
            else:
                if (
                    arguments["enabled"]["enabled"]
                    and await self.service.operation(key) is None
                ):
                    contact = await self._contact(arguments["session_id"])
                    if not contact["ready"]:
                        raise ValueError(
                            "主动联系在执行前已不可用，本次没有恢复跟进："
                            + contact["reason"]
                        )
                await self.service.set_enabled(
                    SetEnabled.model_validate(json_value(arguments["enabled"])),
                    operation_key=key,
                )
            return receipt(await self.service.operation(key))
        except ValueError as error:
            return result({"error": str(error)}, "error")

    async def query(self, key):
        if self.action == "read":
            return None
        recorded = await self.service.operation(key)
        allowed = {"create"} if self.action == "create" else {"set_enabled", "revise"}
        if recorded is not None and recorded["action"] not in allowed:
            raise ValueError("跟进操作标识属于另一种工具")
        return receipt(recorded) if recorded is not None else None

    async def _read(self, arguments):
        overview = await self.service.overview(arguments["session_id"])
        if arguments["watch_id"] is None:
            offset, limit = arguments["offset"], arguments["limit"]
            watches = overview["watches"]
            rows = [watch_summary(row) for row in watches[offset : offset + limit]]
            for row in rows:
                row["purpose"] = row["purpose"][:300]
            return {
                "watches": rows,
                "total": len(watches),
                "offset": offset,
                "next_offset": offset + limit
                if offset + limit < len(watches)
                else None,
                "mailboxes": overview["mailboxes"],
                "proactive": await self._contact(arguments["session_id"]),
            }
        timeline = await self.service.timeline(arguments["watch_id"])
        cursor = timeline["cursor"] or {}
        events = []
        for event in sorted(
            timeline["events"], key=lambda row: row["observed_at"], reverse=True
        )[: min(arguments["limit"], 5)]:
            decisions = event["decisions"]
            decision = next(
                (
                    row
                    for row in reversed(decisions)
                    if row["tool"] in {"share_content", "skip_content"}
                ),
                None,
            )
            # Show original user evidence before recalled assistant commentary.
            # A compact view must still disclose that it is only an excerpt.
            unique = {}
            for row in sorted(
                event["evidence"],
                key=lambda row: (
                    row.get("author") == "user",
                    row.get("recorded_at") or "",
                    row.get("kind") == "source_read",
                ),
                reverse=True,
            ):
                unique.setdefault(row["message_id"], row)
            evidence = list(unique.values())[:6]
            content = str(event["payload"].get("content", ""))
            events.append(
                {
                    "item_id": event["item_id"],
                    "revision": event["revision"],
                    "observed_at": event["observed_at"],
                    "status": event["status"],
                    "title": str(event["payload"].get("title", ""))[:200],
                    "source": {
                        "url": event["payload"].get("url"),
                        "content": content[:1200],
                        "truncated": len(content) > 1200
                        or bool(event["payload"].get("content_truncated")),
                    },
                    "decision": None
                    if decision is None
                    else {
                        "tool": decision["tool"],
                        "message_id": decision["message_id"],
                        "text": str(
                            decision["arguments"].get(
                                "message", decision["arguments"].get("reason", "")
                            )
                        )[:600],
                    },
                    "evidence": [
                        {
                            "message_id": row["message_id"],
                            "author": row.get("author"),
                            "recorded_at": row.get("recorded_at"),
                            "kind": row.get("kind"),
                            "text": row["text"][:600],
                            "truncated": len(row["text"]) > 600
                            or bool(row.get("truncated")),
                        }
                        for row in evidence
                    ],
                    "evidence_total": len(unique),
                    "evidence_truncated": len(unique) > len(evidence),
                    "deliveries": [
                        {
                            "message_id": row["message_id"],
                            "confirmed_at": row["confirmed_at"],
                            "text": row["text"][:600],
                        }
                        for row in event["deliveries"][:3]
                    ],
                }
            )
        value = {
            "watch": watch_summary(timeline["watch"]),
            "proactive": await self._contact(arguments["session_id"]),
            "initialized": bool(cursor.get("initialized")),
            "last_error": cursor.get("last_error"),
            "next_check": cursor.get("next_check"),
            "events": events,
            "events_in_current_window": len(timeline["events"]),
            "note": "source 是接纳时的外部内容，evidence 是调查实际读取的历史节选（优先用户原话）；"
            "截断不表示其余证据不存在。仅 deliveries 中确认的回执表示已送达，不表示已读。",
        }
        while events and len(json.dumps(value, ensure_ascii=False)) > 12000:
            events.pop()
        value["events_truncated"] = len(events) < len(timeline["events"])
        return value


async def register(ctx, service, readiness):
    catalog = ctx.require(TOOLS)
    await catalog.declare_group(
        ctx,
        description=(
            "持续跟进网页、RSS/GitHub 发布及已连接的只读邮件；从聊天创建、查结果、修改规则、暂停或恢复。"
            "查询主动联系是否开启、前置是否就绪、通知发往哪里，也用本组的 read_followups。"
            "每次新交办或恢复均重新读取状态，不能用历史中的未就绪结果推断当前仍不可用。"
            "一次性查资料用网页工具，到时提醒用 scheduler；用户明确交办持续关注时才创建。"
            "答复优先名称、实际状态和用户关心的变化，内部标识无需逐项抄给用户。"
        ),
    )
    for action, name, schema, description in (
        (
            "read",
            "read_followups",
            Read,
            "查询主动联系是否开启、是否就绪及实际通知位置；列出当前对话的跟进及已连接邮箱。"
            "提供 watch_id 可查看读取状态、外部变化、决定、用户历史依据和送达回执。",
        ),
        (
            "create",
            "create_followup",
            Create,
            "按用户明确请求持续跟进信息源；关注规则逐字引用本轮用户原话。自动绑定当前对话与原始消息。"
            "每次先重新用 read_followups 核对当前主动联系就绪及通知位置，不能沿用之前的查询；未启用时提示配置，不擅自打开。"
            "仅使用用户提供或核实的 URL / 已连接邮箱，不接收密码。首次成功读取建基线，新变化才调查。",
        ),
        (
            "manage",
            "manage_followup",
            Manage,
            "按用户要求暂停、恢复或修改当前对话已有跟进；先读取准确 watch_id。"
            "revise 逐字引用本轮新规则，生效于后续新变化；暂停后已接纳调查仍可能完成。"
            "此工具仅确认规则或开关变更；要说明最新页面或投递结果，另用 read_followups 核对。",
        ),
    ):

        @asynccontextmanager
        async def open_tool(_state, action=action):
            yield FollowupTool(service, action, readiness)

        await catalog.register(
            ctx,
            name=name,
            description=description,
            parameters=schema.model_json_schema(),
            open=open_tool,
            idempotent=True,
            parallel=action == "read",
        )
