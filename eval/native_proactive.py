"""Step5 native Content screen/investigate/deliver/ack and memory feedback acceptance."""

import argparse
import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from akashic_sdk import AsyncAkashic, RemoteError

from eval.native_alignment import metrics, until
from kirakira_agent.runtime import (
    configure_step_plan,
    initialize_workspace,
    runtime,
    submit,
)
from kirakira_agent.setup import configure_embedding, configure_proactive
from plugins.akasha.infrastructure.consumption import load_message_nodes
from plugins.delivery.contract import DELIVERY_READ
from plugins.ledger.contract import MESSAGE_CATALOG, Output, ToolCall, ToolResult
from plugins.wake.state import WakeState


async def ingress(core, method, payload):
    # Exercise the shipped SDK, JSON serialization, initialize handshake and
    # real private Unix socket, instead of invoking the handler directly.
    async with await AsyncAkashic.connect(
        str(core.workspace / "akashic.sock")
    ) as client:
        return await client.request(method, payload)


@asynccontextmanager
async def diagnostic_source(instrument):
    reads = []
    body = json.dumps(
        {
            "instrument_id": instrument,
            "report_id": "diagnostic-" + instrument,
            "observed_at": datetime.now(UTC).isoformat(),
            "observations": [
                "连接器导通测试失败",
                "接头重新插紧后导通测试恢复",
                "设备在线指示灯恢复",
            ],
            "cause": "连接线接头松动",
            "repair": "重新插紧两端接头并检查在线指示灯",
        },
        ensure_ascii=False,
    ).encode()

    async def handle(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            first = request.split(b"\r\n", 1)[0]
            reads.append(first.decode())
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/diagnostics/{instrument}", reads


async def evaluate(workspace, key_file, manifest, output):
    initialize_workspace(workspace, profile="web")
    report = {
        "scope": "controlled producer scores and real HTTP evidence, native model/decision/delivery pipeline",
        "max_output_tokens": 8192,
        "cases": [],
    }
    nonce, sid = uuid4().hex[:8], "akashic:owner"
    secret = key_file.read_text().strip()

    async def case(name, operation):
        start = time.monotonic()
        try:
            row = {"name": name, "passed": True, "detail": await operation()}
        except Exception as error:  # noqa: BLE001
            row = {
                "name": name,
                "passed": False,
                "error": (type(error).__name__ + ": " + str(error)).replace(
                    secret, "[redacted]"
                )[:1500],
            }
        row["seconds"] = round(time.monotonic() - start, 3)
        report["cases"].append(row)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v for k, v in row.items() if k != "detail"}), flush=True)

    async with (
        diagnostic_source(nonce) as (evidence_url, evidence_reads),
        runtime(workspace) as core,
    ):
        await configure_step_plan(core, key_file, max_output_tokens=8192)
        await configure_embedding(core, manifest)
        catalog = core.plugin_manager.live_root.context.require(MESSAGE_CATALOG)
        history = core.plugin_manager.live_root.context.require(DELIVERY_READ)
        state = WakeState(workspace / "plugin-data/wake-builtin/wake.sqlite3")

        async def empty_reindex():
            result = await submit(
                core, "akashic:maintenance", "empty-reindex", "/akasha_reindex confirm"
            )
            proof = [
                p.value
                for p in result.body.parts
                if getattr(p, "kind", None) == "command.result"
            ]
            assert proof and proof[0]["kind"] == "success"
            return {"zero_turn_rebuild_completed": True}

        await case("native_empty_graph_reindex", empty_reindex)

        async def reject_invalid():
            base = {
                "event_id": "invalid",
                "observed_at": datetime.now(UTC).isoformat(),
                "payload": {"text": "invalid fixture"},
            }
            for params in (
                {**base, "source_id": "another-plugin"},
                {**base, "observed_at": "2026-10-20T00:00:00"},
            ):
                try:
                    await ingress(core, "event-ingress/context", params)
                except RemoteError as error:
                    assert error.code < 0
                else:
                    raise AssertionError(
                        "invalid producer identity or naive time was accepted"
                    )
            return {"forged_source_rejected": True, "naive_datetime_rejected": True}

        await case("sdk_ingress_rejects_forged_source_and_naive_time", reject_invalid)

        async def remember():
            await submit(
                core,
                sid,
                "interest-" + nonce,
                "请实际调用 remember_memory，message_ids=['current_user_message']，记住本条偏好。"
                + "我维护观测仪器 "
                + nonce
                + "，关心离线原因和连接线修复。"
                + "我的本机只读诊断源是 "
                + evidence_url
                + "。"
                + "以后收到该来源的新报告推送时，请用 web_fetch 核对原文，再主动转告原因和修复步骤。"
                + "当前这条消息只登记偏好，然后回复已记住。",
            )
            await until(
                lambda: (
                    "interest-" + nonce
                    in load_message_nodes(workspace / "memory/akasha.db")
                )
            )
            rows = catalog.reader(sid).snapshot()
            bindings = catalog.reader(sid).read_page(limit=100).bindings
            assert any(
                isinstance(p, ToolCall)
                and bindings[p.binding_id]["metadata"]["tool"]["name"]
                == "remember_memory"
                for row in rows
                if isinstance(row.body, Output)
                for p in row.body.parts
            )
            with closing(
                sqlite3.connect(
                    (workspace / "memory/akasha.db").as_uri() + "?mode=ro", uri=True
                )
            ) as db:
                count = db.execute(
                    "SELECT count(*) FROM feedback_events WHERE action='remember'"
                ).fetchone()[0]
            assert count >= 1
            return {"published_remember_feedback": count}

        await case("real_remember_tool_is_consumed_with_turn", remember)
        await configure_proactive(core, workspace, sid)

        async def share():
            reads_before = len(evidence_reads)
            now = datetime.now(UTC)
            await ingress(
                core,
                "event-ingress/content",
                {
                    "batch_id": "confirmed-" + nonce,
                    "items": [
                        {
                            "item_id": "repair-" + nonce,
                            "revision": "1",
                            "payload": {
                                "title": "观测仪器 " + nonce + " 的诊断记录已更新",
                                "url": evidence_url,
                                "content": "新增连接器导通测试、接头重插前后与在线指示灯的观测记录。完整诊断报告见来源链接。",
                                "published_at": now.isoformat(),
                                "preprocess_score": 0.999,
                            },
                        }
                    ],
                },
            )

            def completed_delivery():
                for run in state.list_runs(10):
                    if run.get("owner") == "content" and run.get("decision") in {
                        "skip",
                        "defer",
                        "invalidated",
                    }:
                        raise AssertionError(
                            "native content decision: "
                            + str(run.get("decision"))
                            + ": "
                            + str(run.get("decision_detail"))
                        )
                return next(
                    (
                        m
                        for m in catalog.reader(sid).snapshot()
                        if m.source == "wake" and isinstance(m.body, Output)
                    ),
                    None,
                )

            row = await until(completed_delivery, timeout=180)
            await until(
                lambda: (
                    v
                    if (v := history.status(row.message_id, "akashic"))
                    and (v.get("receipt") or {}).get("status") == "delivered"
                    else None
                )
            )
            runs = state.list_runs(10)
            assert runs and any(r.get("owner") == "content" for r in runs)
            # Read exact native internal calls; ordinary final text cannot fake a decision.
            names = []
            for session in catalog.snapshot_heads():
                reader = catalog.reader(session)
                bindings = reader.read_page(limit=100).bindings
                for message in reader.snapshot():
                    if message.source == "wake" and isinstance(message.body, Output):
                        names.extend(
                            bindings[p.binding_id]["metadata"]["tool"]["name"]
                            for p in message.body.parts
                            if isinstance(p, ToolCall)
                        )
            assert "screen_content" in names and "share_content" in names
            assert "skip_content" not in names
            assert "web_fetch" in names and len(evidence_reads) > reads_before
            assert evidence_url in str(row.body.parts)
            return {
                "delivered": 1,
                "screen_tool": True,
                "share_tool": True,
                "real_http_verifications": len(evidence_reads) - reads_before,
                "source_link_preserved": True,
                "producer_preprocess_score": 0.999,
                "score_is_controlled_not_quality_metric": True,
            }

        await case("content_screen_investigate_share_delivery", share)

        async def acknowledge():
            async def pending():
                for _ in range(200):
                    items = (await ingress(core, "event-ingress/unsettled", {}))[
                        "items"
                    ]
                    if items:
                        return items
                    await asyncio.sleep(0.05)
                raise TimeoutError("producer did not receive content settlement")

            items = await pending()
            ref = items[0]["settlement_ref"]
            first = await ingress(core, "event-ingress/ack", {"settlement_ref": ref})
            second = await ingress(core, "event-ingress/ack", {"settlement_ref": ref})
            assert first["settled"] and second["settled"] and second["duplicate"]
            assert not (await ingress(core, "event-ingress/unsettled", {}))["items"]
            return {"pending_after_ack": 0, "repeated_ack_is_idempotent": True}

        await case("producer_settlement_and_duplicate_ack", acknowledge)

        async def forget():
            await submit(
                core,
                sid,
                "forget-" + nonce,
                "请实际调用 forget_memory，message_ids=['interest-" + nonce + "']，"
                "撤回这条历史偏好对后续关联的影响，不删除原始记录，然后简短确认。",
            )
            await until(
                lambda: (
                    "forget-" + nonce
                    in load_message_nodes(workspace / "memory/akasha.db")
                )
            )
            with closing(
                sqlite3.connect(
                    (workspace / "memory/akasha.db").as_uri() + "?mode=ro", uri=True
                )
            ) as db:
                count = db.execute(
                    "SELECT count(*) FROM feedback_events WHERE action='forget'"
                ).fetchone()[0]
            assert count >= 1
            assert catalog.reader(sid).get("interest-" + nonce) is not None
            return {
                "published_forget_feedback": count,
                "original_message_preserved": True,
            }

        await case("real_forget_feedback_preserves_source_fact", forget)
        assert any(
            isinstance(m.body, ToolResult) for m in catalog.reader(sid).snapshot()
        )
    report["models"] = metrics(workspace)
    report["summary"] = {
        "passed": sum(r["passed"] for r in report["cases"]),
        "total": len(report["cases"]),
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return all(r["passed"] for r in report["cases"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--embedding-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.environ.setdefault(
        "AKASHIC_PLUGIN_HOME",
        str(args.workspace.parent / (args.workspace.name + "-home")),
    )
    ok = asyncio.run(
        evaluate(
            args.workspace.resolve(),
            args.key_file,
            args.embedding_manifest,
            args.output,
        )
    )
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
