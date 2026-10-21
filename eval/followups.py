"""Conversation -> feed -> native investigation -> confirmed delivery acceptance.

Controlled replay is functional evidence, not a real-user notification quality rate.
No event contains the answer, synthetic interest score or a mandatory Alert.
Explicit owner requests admit review; learned interest is measured separately.
"""

import argparse
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from uuid import uuid4

from eval.models import EvaluationModel, provider_slot, source_identity
from eval.native_alignment import metrics, until
from kirakira_agent.runtime import initialize_workspace, rpc, runtime, submit
from kirakira_agent.setup import configure_embedding, configure_proactive
from plugins.akasha.contract import SEMANTIC_INTEREST
from plugins.akasha.infrastructure.consumption import load_message_nodes
from plugins.ledger.contract import MESSAGE_CATALOG
from plugins.ui.contract import PLUGIN_UI


@asynccontextmanager
async def source():
    state = {"entries": [], "requests": [], "version": 1, "pages": {}}

    async def handle(reader, writer):
        try:
            request = (
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            ).decode()
            first = request.split("\r\n", 1)[0]
            state["requests"].append(first)
            path = first.split(" ")[1]
            etag = '"' + str(state["version"]) + '"'
            unchanged = (
                path in {"/feed", "/watched"}
                and ("if-none-match: " + etag) in request.lower()
            )
            if path == "/feed":
                body = (
                    '<feed xmlns="http://www.w3.org/2005/Atom">'
                    + "".join(
                        "<entry><id>"
                        + escape(row["id"])
                        + "</id><title>"
                        + escape(row["title"])
                        + "</title><summary>"
                        + escape(row["content"])
                        + "</summary><published>"
                        + row["published_at"]
                        + '</published><link href="'
                        + escape(state["url"] + "/" + row["id"], quote=True)
                        + '"/></entry>'
                        for row in state["entries"]
                    )
                    + "</feed>"
                ).encode()
                content_type = "application/atom+xml"
            elif path == "/watched":
                content = state["pages"].get(
                    path, "<h1>云杉库发布记录</h1><p>暂无新公告。</p>"
                )
                body = (
                    "<!doctype html><html><head><meta charset='utf-8'><title>云杉库发布记录</title></head><body><nav>导航</nav><main>"
                    + content
                    + "</main></body></html>"
                ).encode()
                content_type = "text/html; charset=utf-8"
            else:
                body = state["pages"].get(path, "No additional facts.").encode()
                content_type = "text/plain; charset=utf-8"
            if unchanged:
                writer.write(b"HTTP/1.1 304 Not Modified\r\nConnection: close\r\n\r\n")
            else:
                writer.write(
                    (
                        f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nETag: {etag}\r\nConnection: close\r\n\r\n"
                    ).encode()
                    + body
                )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state["url"] = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()


async def evaluate(args):
    model = EvaluationModel.read(args.model_config)
    initialize_workspace(
        args.workspace, profile="web" if args.web or args.isolated else "headless"
    )
    report = {
        "source_kind": "webpage" if args.webpage else "feed",
        "model_config": model.description(),
        "implementation": source_identity(),
        "scope": "controlled functional replay, not population notification precision",
        "cases": [],
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    async def case(name, action):
        start = time.monotonic()
        try:
            row = {"name": name, "passed": True, "detail": await action()}
        except Exception as error:  # noqa: BLE001 - preserve independent acceptance failures
            import traceback

            traceback.print_exc()
            row = {
                "name": name,
                "passed": False,
                "error": type(error).__name__ + ": " + str(error)[:1500],
            }
        row["seconds"] = round(time.monotonic() - start, 3)
        report["cases"].append(row)
        save()
        print(
            json.dumps({key: value for key, value in row.items() if key != "detail"}),
            flush=True,
        )
        return row["passed"]

    sid = "akashic:followups-owner"
    destination = "akashic:followups-inbox" if args.isolated else sid
    watch_id = "w_" + uuid4().hex
    async with source() as feed:
        async with runtime(args.workspace) as core:

            async def configure():
                failures = []
                for attempt in range(3):
                    try:
                        await model.configure(core, args.key_file)
                        return {"attempts": attempt + 1, "previous_failures": failures}
                    except RuntimeError as error:
                        if "对话验证没有返回文字" not in str(error):
                            raise
                        failures.append("provider validation returned no text")
                raise RuntimeError(
                    "provider validation returned no text in all three attempts"
                )

            if not await case("configure_real_provider", configure):
                return False
            await configure_embedding(core, args.embedding_manifest)
            ctx = core.plugin_manager.live_root.context
            catalog = ctx.require(MESSAGE_CATALOG)
            main_project, decoy_project = "p_" + uuid4().hex, "p_" + uuid4().hex
            if args.isolated:
                slots = ctx.require(PLUGIN_UI)
                entries = {row["id"]: row for row in (await slots.catalog())["items"]}
                for project, name in ((main_project, "青禾"), (decoy_project, "栖云")):
                    await slots.query(
                        "projects",
                        entries["projects"]["revision"],
                        "project.create",
                        {"project_id": project, "name": name},
                        session_id=None,
                        turn_id=None,
                    )
                    await slots.query(
                        "akasha",
                        entries["akasha"]["revision"],
                        "scope.policy.set",
                        {"dimension": "project", "value": project, "learn": "isolated"},
                        session_id=None,
                        turn_id=None,
                    )
                await submit(
                    core,
                    destination,
                    "inbox-setup",
                    "这个对话用于接收通知。请只回复已准备好。",
                )
            training = [
                (
                    sid,
                    "owner-request",
                    (
                        "我在青禾研究项目使用云杉库，关注云杉库的离线备份、缓存格式和版本升级兼容性。"
                        "请用 remember_memory，message_ids=['current_user_message']，记住本条关注。现在不允许升级到 v2。"
                        "以后只有实际影响离线数据或备份的变化才值得提醒，文案与排版改动不要提醒。然后只回复已记住。"
                    ),
                ),
                (
                    "akashic:followups-amendment",
                    "owner-amendment",
                    (
                        "更正青禾研究项目对云杉库 v2 的安排：现在允许升级，"
                        "但必须先导出加密离线备份，再在隔离副本验证旧缓存可读，不能直接迁移生产数据。负责人是顾予安。"
                        "此前不允许 v2 的约定作废。请只回复已记下。"
                    ),
                ),
                (
                    "akashic:followups-decoy",
                    "owner-decoy",
                    (
                        "栖云研究项目也用云杉库，但它只保留公开测试数据，"
                        "可直接清空缓存升级 v2，不需要加密备份，负责人周明。这个项目和青禾的限制不同。请只回复已记下。"
                    ),
                ),
            ]
            for session, identity, prompt in training:
                metadata = (
                    {
                        "session_dimensions": {
                            "project": decoy_project
                            if identity == "owner-decoy"
                            else main_project
                        }
                    }
                    if args.isolated
                    else None
                )
                await submit(core, session, identity, prompt, metadata=metadata)
                print(json.dumps({"history_completed": identity}), flush=True)

            def learned():
                nodes = {}
                for path in (args.workspace / "memory").rglob("akasha.db"):
                    nodes.update(load_message_nodes(path))
                return all(identity in nodes for _, identity, _ in training)

            await until(learned, timeout=180)

            async def create():
                spec = {
                    "watch_id": watch_id,
                    "session_id": sid,
                    "name": "青禾 · 云杉库升级",
                    "purpose": "关注影响青禾研究项目离线备份、缓存格式和版本升级兼容性的实质变化；"
                    "只改文档排版可跳过。结合后来的项目更正判断。",
                    "url": feed["url"] + ("/watched" if args.webpage else "/feed"),
                    "source_kind": "webpage" if args.webpage else "feed",
                    "anchors": ["owner-request"],
                    "interval_seconds": 3600,
                }
                value = await rpc(core, "followups/create", spec)
                assert (await rpc(core, "followups/create", spec)) == value
                overview = await rpc(core, "followups/overview", {"session_id": sid})
                assert overview["anchors"][0]["message_id"] == "owner-request"
                return {
                    "watch_id": watch_id,
                    "actual_user_message": True,
                    "retry_same_watch": True,
                }

            await case("create_from_original_user_conversation", create)

            async def baseline():
                # The native polling task can race this manual check; both share the same lock.
                result = await rpc(core, "followups/poll", {"watch_id": watch_id})
                value = await rpc(core, "followups/timeline", {"watch_id": watch_id})
                assert value["cursor"]["initialized"] and not value["events"]
                return {"initial_poll": result, "historical_notifications": 0}

            await case("first_fetch_establishes_quiet_baseline", baseline)
            await configure_proactive(core, args.workspace, destination)

            async def event(identity, title, body, expected):
                feed["entries"] = [
                    {
                        "id": identity,
                        "title": title,
                        "content": body,
                        "published_at": datetime.now(UTC).isoformat(),
                    }
                ]
                feed["pages"]["/" + identity] = title + "\n" + body
                if args.webpage:
                    feed["pages"]["/watched"] = (
                        "<h1>" + escape(title) + "</h1><p>" + escape(body) + "</p>"
                    )
                feed["version"] += 1
                semantic = await ctx.require(SEMANTIC_INTEREST).score(
                    [title + "\n" + body], cutoff=datetime.now(UTC).isoformat()
                )
                result = await rpc(core, "followups/poll", {"watch_id": watch_id})
                assert result["submitted"] == 1

                async def wait():
                    while True:
                        timeline = await rpc(
                            core, "followups/timeline", {"watch_id": watch_id}
                        )
                        current = next(
                            row
                            for row in timeline["events"]
                            if row["item_id"] == result["refs"][0]["item_id"]
                            and row["revision"] == result["refs"][0]["revision"]
                        )
                        terminal = [
                            row
                            for row in current["decisions"]
                            if row["tool"] in {"share_content", "skip_content"}
                        ]
                        if terminal and (
                            terminal[-1]["tool"] == "skip_content"
                            or current["deliveries"]
                        ):
                            return current
                        await asyncio.sleep(0.25)

                try:
                    current = await asyncio.wait_for(wait(), 240)
                except TimeoutError:
                    report.setdefault("unfinished_events", []).append(
                        {
                            "identity": identity,
                            "semantic_interest": semantic,
                            "timeline": await rpc(
                                core, "followups/timeline", {"watch_id": watch_id}
                            ),
                        }
                    )
                    raise
                report.setdefault("event_results", []).append(
                    {
                        "identity": identity,
                        "semantic_interest": semantic,
                        "event": current,
                    }
                )
                terminal = [
                    row["tool"]
                    for row in current["decisions"]
                    if row["tool"] in {"share_content", "skip_content"}
                ]
                assert terminal[-1] == expected, terminal
                assert "preprocess_score" not in current["payload"]
                if expected == "share_content":
                    message = "\n".join(row["text"] for row in current["deliveries"])
                    assert "加密" in message and (
                        "隔离" in message or "副本" in message
                    ), message
                    assert "不允许升级" not in message and "周明" not in message, (
                        message
                    )
                    assert any(
                        row["message_id"] == "owner-amendment"
                        for row in current["evidence"]
                    ), current["evidence"]
                    assert current["deliveries"] and feed["url"] in message
                else:
                    assert not current["deliveries"]
                return {
                    "semantic_interest": semantic,
                    "decision": expected,
                    "deliveries": len(current["deliveries"]),
                    "memory_evidence": len(current["evidence"]),
                    "original_event": result["refs"],
                }

            await case(
                "cosmetic_update_is_investigated_and_skipped",
                lambda: event(
                    "cosmetic",
                    "云杉库离线备份与缓存升级兼容性文档排版更新",
                    "本次只调整离线备份、缓存格式、版本升级兼容性章节的目录和字体；"
                    "程序、缓存格式、导出接口和迁移规则均未变化，没有数据风险，无需用户操作。",
                    "skip_content",
                ),
            )
            await case(
                "new_event_uses_amended_history_and_confirms_delivery",
                lambda: event(
                    "migration",
                    "云杉库 v2 离线备份与缓存格式升级兼容性公告",
                    "v2 的存储格式发生变更，首次启动会原位转换 v1 缓存，转换后 v1 无法再读。"
                    "离线备份导出功能保留，升级前应检查自己的数据保护和验证要求。这个公告不包含任何个人项目安排。",
                    "share_content",
                ),
            )

            if args.isolated:

                async def scope():
                    value = await rpc(
                        core, "followups/timeline", {"watch_id": watch_id}
                    )
                    sessions = {
                        change["detail"]["accepted_turn"]["session_id"]
                        for event in value["events"]
                        for change in event["history"]
                        if change["action"] == "selected"
                    }
                    assert len(sessions) >= 2
                    for session in sessions:
                        attributes = catalog.attributes(session)
                        assert (
                            attributes.dimension("project") == main_project
                            and attributes.learning == "excluded"
                        )
                        request = next(
                            part.value
                            for message in catalog.reader(session).snapshot()
                            for part in getattr(message.body, "parts", ())
                            if getattr(part, "kind", None) == "wake.request"
                        )
                        assert len(request["items"]) == 1
                        assert (
                            request["items"][0]["payload"]["context_session_id"] == sid
                        )
                    assert catalog.attributes(destination).scope == ()
                    assert all(
                        row["message_id"] != "owner-decoy"
                        for event in value["events"]
                        for row in event["evidence"]
                    )
                    return {
                        "investigation_sessions": len(sessions),
                        "context_project_inherited": True,
                        "unscoped_delivery_inbox": True,
                        "other_isolated_project_sources": 0,
                    }

                await case(
                    "investigation_scope_is_independent_of_delivery_location", scope
                )

            async def duplicate():
                before = await rpc(core, "followups/timeline", {"watch_id": watch_id})
                result = await rpc(core, "followups/poll", {"watch_id": watch_id})
                after = await rpc(core, "followups/timeline", {"watch_id": watch_id})
                assert result["submitted"] == 0 and after["events"] == before["events"]
                return {
                    "duplicate_submissions": 0,
                    "confirmed_deliveries": sum(
                        len(row["deliveries"]) for row in after["events"]
                    ),
                }

            await case(
                "etag_replay_does_not_repeat_investigation_or_delivery", duplicate
            )

            async def feedback():
                value = await rpc(core, "followups/timeline", {"watch_id": watch_id})
                event = next(row for row in value["events"] if row["deliveries"])
                await rpc(
                    core,
                    "followups/feedback",
                    {
                        "watch_id": watch_id,
                        "item_id": event["item_id"],
                        "revision": event["revision"],
                        "value": "useful",
                    },
                )
                await rpc(
                    core,
                    "followups/set-enabled",
                    {"watch_id": watch_id, "enabled": False},
                )
                count = len(feed["requests"])
                assert (await rpc(core, "followups/poll", {"watch_id": watch_id}))[
                    "status"
                ] == "paused"
                assert len(feed["requests"]) == count
                report["final_timeline"] = await rpc(
                    core, "followups/timeline", {"watch_id": watch_id}
                )
                return {
                    "actual_feedback_persisted": True,
                    "feedback_is_not_automatic_model_learning": True,
                    "paused_stops_new_http_reads": True,
                }

            await case("feedback_and_pause_are_real_owner_controls", feedback)

            if args.long_source:

                async def long_evidence():
                    long_session = "akashic:followups-long-report"
                    message_id = "long-formal-report"
                    history = (
                        "梧桐项目的正式结项报告。最终签字人仅写在末尾署名页，本段没有姓名。\n"
                        + "Background context without final signatures. " * 450
                        + "\n正式署名页：许亦澄。\n请只回复已记下，不转述姓名，不写档案文件。"
                    )
                    metadata = (
                        {"session_dimensions": {"project": main_project}}
                        if args.isolated
                        else None
                    )
                    await submit(
                        core, long_session, message_id, history, metadata=metadata
                    )

                    async def learned_long():
                        while not any(
                            message_id in load_message_nodes(path)
                            for path in (args.workspace / "memory").rglob("akasha.db")
                        ):
                            await asyncio.sleep(0.1)

                    await asyncio.wait_for(learned_long(), 180)
                    identity = "w_" + uuid4().hex
                    report["long_watch_id"] = identity
                    async with source() as document_feed:
                        await rpc(
                            core,
                            "followups/create",
                            {
                                "watch_id": identity,
                                "session_id": long_session,
                                "name": "梧桐 · 署名核对",
                                "purpose": "签署流程更新时，请核对正式结项报告末尾的签字人并告诉我。"
                                "请先以 presentation='excerpts'、selection='fused' 召回报告；摘录不含署名页时，沿凭据展开原文末尾。"
                                "不能拿项目负责人代替签字人。",
                                "url": document_feed["url"] + "/feed",
                                "anchors": [message_id],
                                "interval_seconds": 3600,
                            },
                        )
                        try:
                            await rpc(core, "followups/poll", {"watch_id": identity})
                            document_feed["entries"] = [
                                {
                                    "id": "signature",
                                    "title": "梧桐项目结项材料签署流程已更新",
                                    "content": "新版流程要求核对正式归档报告的最终签字人。姓名以项目已有正式报告末尾的署名页为准，公告不包含姓名。",
                                    "published_at": datetime.now(UTC).isoformat(),
                                }
                            ]
                            document_feed["version"] += 1
                            document_feed["pages"]["/signature"] = document_feed[
                                "entries"
                            ][0]["content"]
                            await rpc(core, "followups/poll", {"watch_id": identity})

                            async def delivered():
                                while True:
                                    value = await rpc(
                                        core,
                                        "followups/timeline",
                                        {"watch_id": identity},
                                    )
                                    if (
                                        value["events"]
                                        and value["events"][0]["deliveries"]
                                    ):
                                        return value
                                    if any(
                                        decision["tool"] == "skip_content"
                                        for event in value["events"]
                                        for decision in event["decisions"]
                                    ):
                                        raise AssertionError(
                                            "source verification was skipped"
                                        )
                                    await asyncio.sleep(0.25)

                            value = await asyncio.wait_for(delivered(), 240)
                            event = value["events"][0]
                            proofs = [
                                row
                                for row in event["evidence"]
                                if row["kind"] == "source_read"
                                and row["message_id"] == message_id
                            ]
                            assert proofs and any(row["start"] > 0 for row in proofs)
                            assert "许亦澄" in "\n".join(
                                row["text"] for row in event["deliveries"]
                            )
                            report["long_event"] = event
                            return {
                                "original_chars": len(history),
                                "original_tail_reads": len(proofs),
                                "source_ranges": [
                                    {
                                        key: row[key]
                                        for key in (
                                            "message_id",
                                            "start",
                                            "end",
                                            "source_chars",
                                        )
                                    }
                                    for row in proofs
                                ],
                                "confirmed_deliveries": len(event["deliveries"]),
                                "tool_strategy_explicitly_requested": True,
                                "event_contains_answer": False,
                            }
                        finally:
                            await rpc(
                                core,
                                "followups/set-enabled",
                                {"watch_id": identity, "enabled": False},
                            )

                await case(
                    "proactive_investigation_expands_long_original_evidence",
                    long_evidence,
                )

            report["metrics"] = metrics(args.workspace, model)

        async with runtime(args.workspace) as core:

            async def restart():
                value = await rpc(core, "followups/timeline", {"watch_id": watch_id})
                assert not value["watch"]["enabled"]
                assert value["events"] == report["final_timeline"]["events"]
                assert any(
                    row["feedback"] and row["feedback"]["value"] == "useful"
                    for row in value["events"]
                )
                if "long_event" in report:
                    long = await rpc(
                        core,
                        "followups/timeline",
                        {"watch_id": report["long_watch_id"]},
                    )
                    assert (
                        long["events"][0] == report["long_event"]
                        and not long["watch"]["enabled"]
                    )
                return {
                    "same_event_receipts": True,
                    "feedback_and_pause_preserved": True,
                }

            await case(
                "restart_preserves_watch_evidence_feedback_and_delivery", restart
            )
    save()
    return all(row["passed"] for row in report["cases"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--embedding-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--long-source", action="store_true")
    parser.add_argument("--webpage", action="store_true")
    args = parser.parse_args()
    os.environ["AKASHIC_PLUGIN_HOME"] = str(
        args.workspace.parent / (args.workspace.name + "-home")
    )

    async def run():
        async with provider_slot(
            EvaluationModel.read(args.model_config), args.key_file
        ):
            return await evaluate(args)

    raise SystemExit(0 if asyncio.run(run()) else 1)


if __name__ == "__main__":
    main()
