"""Real-model idle initiative, evidence, quiet decisions and restart acceptance."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from agent.plugin_composition.config_input import save_config
from core.common.file_io import run_file_io
from eval.models import EvaluationModel, provider_slot, source_identity
from eval.native_alignment import metrics, until
from kirakira_agent.runtime import ROOT, initialize_workspace, rpc, runtime, submit
from kirakira_agent.setup import configure_embedding, configure_proactive
from plugins.akasha.contract import AKASHA_RECORDS_VIEW
from plugins.akasha.infrastructure.consumption import load_message_nodes
from plugins.delivery.contract import DELIVERY_READ
from plugins.drift.contract import DRIFT_INITIATIVE, DRIFT_WAKE_V2
from plugins.ledger.contract import MESSAGE_CATALOG, Output, ToolResult
from plugins.ui.contract import PLUGIN_UI


async def evaluate(args):
    if args.workspace.exists():
        raise ValueError("use a fresh local workspace")
    initialize_workspace(args.workspace, profile="web")
    source, inbox = "akashic:idle-" + uuid4().hex, "akashic:idle-inbox"
    save_config(args.workspace / "plugin-data/drift-builtin", {
        "auto_enabled": False, "context_session_id": source, "daily_budget": 1,
        "idle_seconds": 0, "interval_seconds": 60,
        "purpose": ("结合近期交流和过去经历，找出一件值得继续整理或研究的事；没有新增价值就保持安静。"
                    if args.implicit_goal else "核对我尚未完成的概率论学习资料选择，按当前时间条件和预算研究；已完成或撤回就保持安静。"),
    })
    model = EvaluationModel.read(args.model_config)
    report = {"implementation": source_identity(), "model_config": model.description(),
              "scope": "controlled personal learning scenario; no incoming external event; not population initiative usefulness",
              "cases": [], "source_session": source, "inbox_session": inbox, "implicit_goal": args.implicit_goal}
    for directory in ("plugins/drift",):
        report["implementation"].update({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in (ROOT / directory).glob("*.py")})
    report["implementation"]["eval/idle_reflection.py"] = hashlib.sha256((ROOT / "eval/idle_reflection.py").read_bytes()).hexdigest()

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    async def case(name, operation):
        started = time.monotonic()
        try:
            row = {"name": name, "passed": True, "detail": await operation()}
        except Exception as error:  # noqa: BLE001 - preserve independent actual failures
            row = {"name": name, "passed": False, "error": type(error).__name__ + ": " + str(error)[:1200]}
        row["seconds"] = round(time.monotonic() - started, 3)
        report["cases"].append(row)
        save()
        print(json.dumps({k: v for k, v in row.items() if k != "detail"}), flush=True)
        return row["passed"]

    requests = []

    async def page(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request.split(b"\r\n", 1)[0].decode())
            body = ("<html><body><h1>松风概率论学习资料</h1>"
                    "<p>资料甲：80元，周三到场学习，课程已确认仅工作日开放。</p>"
                    "<p>资料乙：90元，周六线上学习，内容涵盖条件概率与贝叶斯公式。</p>"
                    "</body></html>").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(page, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/probability"
    try:
        async with runtime(args.workspace) as core:
            async def configure():
                await model.configure(core, args.key_file)
                await configure_embedding(core, args.embedding_manifest)
                await submit(core, inbox, "inbox-ready", "这里只收主动消息，请只回复收到。")
                await configure_proactive(core, args.workspace, inbox)
                return {"models": "native configured driver", "embedding": "local", "external_events_submitted": 0}

            if not await case("configure_native_memory_and_delivery", configure):
                return False

            async def scope():
                ui = core.plugin_manager.live_root.context.require(PLUGIN_UI)
                entries = {r["id"]: r for r in (await ui.catalog())["items"]}
                project = "p_" + uuid4().hex
                for plugin, method, value in (
                    ("projects", "project.create", {"project_id": project, "name": "个人学习"}),
                    ("akasha", "scope.policy.set", {"dimension": "project", "value": project, "learn": "isolated"}),
                ):
                    await ui.query(plugin, entries[plugin]["revision"], method, value, session_id=None, turn_id=None)
                metadata = {"session_dimensions": {"project": project}}
                if args.implicit_goal:
                    await submit(core, source + "-preferences", "learning-conditions",
                                 "概率论学习资料的开销最多100元，我每周只有周末有空，线上也可以；工作日到场的课程不考虑。请只回复已记下。", metadata=metadata)
                    await until(lambda: any("learning-conditions" in load_message_nodes(p)
                                            for p in (args.workspace / "memory").rglob("akasha.db")), timeout=180)
                goal = (
                    f"松风概率论学习资料我还没有选，候选页面在{url}。这件事还没处理，我先去忙了，这条只回复收到，不展开答复。"
                    if args.implicit_goal else (
                    "我最近想学概率论，正在选松风的学习资料，预算100元，只能周末参加，线上可以。"
                    f"还没核对的页面是{url}。这次请只回复收到，不要调查或给方案；留到空闲思考时再研究哪个合适。"
                    ))
                await submit(core, source, "learning-goal", goal, metadata=metadata)
                await until(lambda: any("learning-goal" in load_message_nodes(p)
                                        for p in (args.workspace / "memory").rglob("akasha.db")), timeout=180)
                report["project"] = project
                return {"project": project, "source_and_inbox_differ": source != inbox, "goal_learned": True}

            if not await case("unfinished_learning_goal_uses_an_isolated_source_scope", scope):
                return False

            auth = {"Authorization": "Bearer " + (args.workspace / "web-auth/secret").read_text().strip()}
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(args.workspace / "runtime/web-chat.sock")), base_url="http://native", headers=auth) as chat, httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(args.workspace / "runtime/dashboard.sock")), base_url="http://native", headers=auth) as dashboard:
                async def settings(values=None):
                    bootstrap = (await chat.get("/api/chat/web-ui/bootstrap")).json()
                    module = next((m for m in bootstrap["modules"] if m["pluginId"] == "drift"), None)
                    if module is None:
                        raise LookupError("settings module is being replaced")
                    headers = {"X-Akashic-Web-Snapshot": bootstrap["snapshotId"], "X-Akashic-Web-Catalog": bootstrap["catalogId"],
                               "X-Akashic-Web-Module": "drift", "X-Akashic-Web-Generation": module["generationId"]}
                    current = await dashboard.get("/api/dashboard/drift/config", headers=headers)
                    current.raise_for_status()
                    if values is None:
                        return current.json()
                    result = await dashboard.post("/api/dashboard/drift/config", headers=headers,
                        json={"request_id": uuid4().hex, "expected_input": current.json()["input_ref"], "values": values})
                    result.raise_for_status()
                    async with asyncio.timeout(60):
                        while True:
                            try:
                                latest = await settings()
                                if latest["input_ref"] != current.json()["input_ref"]:
                                    return latest
                            except (httpx.HTTPError, LookupError):
                                pass
                            await asyncio.sleep(0.1)

                async def status():
                    return await rpc(core, "drift/status", {})

                async def wait_run(input_id, *, timeout=180):
                    async with asyncio.timeout(timeout):
                        while True:
                            value = await status()
                            found = next((r for r in value["runs"] if r["source_input_id"] == input_id
                                          and (r["status"] not in {"accepted", "planned"} or r.get("handoff_error_type"))), None)
                            if found is not None:
                                return found
                            await asyncio.sleep(0.1)

                async def initiative():
                    changed = await settings({"auto_enabled": True})
                    assert changed["values"]["auto_enabled"] is True
                    value = await wait_run("learning-goal")
                    report["automatic_plan"] = value
                    assert value["automatic"] is True and value["status"] in {"offered", "settled"}, value
                    assert "learning-goal" in value["plan"]["evidence_message_ids"], value
                    assert value["proposal"]["payload"]["context_session_id"] == source
                    if args.implicit_goal:
                        record = core.plugin_manager.live_root.context.require(AKASHA_RECORDS_VIEW)().read(value["retrieval_ref"])
                        assert "learning-conditions" in record.presented_message_ids, "historical conditions were not actually presented"
                        report["planning_memory"] = record.model_dump(mode="json")
                    return value

                if not await case("idle_time_generates_a_grounded_proposal_without_external_events", initiative):
                    report["debug_status"] = await status()
                    return False

                def notifications():
                    context = core.plugin_manager.live_root.context
                    catalog, history = context.require(MESSAGE_CATALOG), context.require(DELIVERY_READ)
                    return [m for m in catalog.reader(inbox).snapshot() if isinstance(m.body, Output)
                            and m.source == "wake" and history.confirmed(m.message_id) is not None]

                async def delivered():
                    await until(notifications, timeout=240)
                    context = core.plugin_manager.live_root.context
                    message = notifications()[-1]
                    body = "\n".join(str(p.value) for p in message.body.parts if p.kind == "text")
                    report["notification"] = {"message_id": message.message_id, "text": body}
                    assert "乙" in body and "90" in body and "周六" in body, body
                    assert requests, "native research did not read the actual page"
                    source_catalog = context.require(MESSAGE_CATALOG)
                    flows = [s for s in source_catalog.snapshot_heads(prefix="wake:")]
                    recalls = [m for s in flows for m in source_catalog.reader(s).snapshot()
                               if isinstance(m.body, ToolResult) and m.body.outcome == "success"
                               and any(p.kind == "akasha.recall" for p in m.body.parts)]
                    assert recalls, "native Drift did not verify history through the memory tool"
                    assert all(source_catalog.attributes(s).scope == source_catalog.attributes(source).scope for s in flows)
                    return {"message_id": message.message_id, "confirmed_deliveries": len(notifications()),
                            "successful_memory_reads": len(recalls), "actual_page_requests": len(requests), "source_scope_inherited": True}

                await case("native_drift_checks_history_researches_and_confirms_delivery", delivered)

                async def unchanged():
                    before = metrics(args.workspace, model)["remote_chat_calls"]
                    value = await rpc(core, "drift/run", {})
                    assert value["status"] in {"unchanged", "pending_proposal"}, value
                    assert metrics(args.workspace, model)["remote_chat_calls"] == before
                    return value

                await case("unchanged_discussion_does_not_repeat_planning", unchanged)

                async def completed():
                    await submit(core, source, "goal-completed", "这件学习资料选择已经完成，我已选好并报名。请停止研究这个话题，不要再查页面或提醒我；这次只回复收到。")
                    before = metrics(args.workspace, model)["remote_chat_calls"]
                    blocked = await rpc(core, "drift/run", {})
                    assert blocked["status"] == "daily_budget", blocked
                    assert metrics(args.workspace, model)["remote_chat_calls"] == before
                    await settings({"daily_budget": 2})
                    value = await rpc(core, "drift/run", {})
                    report["completed_goal_plan"] = value
                    assert value["status"] == "skipped" and value["plan"]["decision"] == "skip", value
                    assert len(notifications()) == 1
                    return {"budget_blocks_without_model_calls": True, "completed_goal_skipped": True, "confirmed_deliveries": 1}

                await case("daily_budget_and_completed_goal_keep_the_agent_quiet", completed)

                async def pause():
                    await settings({"auto_enabled": False})
                    context = core.plugin_manager.live_root.context
                    before = metrics(args.workspace, model)["remote_chat_calls"]
                    value = await context.require(DRIFT_INITIATIVE).run_once()
                    assert value["status"] == "disabled", value
                    assert metrics(args.workspace, model)["remote_chat_calls"] == before
                    report["before_restart"] = await status()
                    report["before_restart_calls"] = before
                    return value

                await case("pause_stops_new_automatic_plans", pause)

                if args.fault_cases:
                    async def handoff_fault():
                        from plugins.wake.api import Config as WakeConfig

                        save_config(args.workspace / "plugin-data/wake-builtin", WakeConfig(
                            enabled=False, delivery={"channel": "akashic", "recipient": inbox.removeprefix("akashic:"), "session_id": inbox}
                        ).model_dump(mode="json"))
                        await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"wake"}))
                        recovery_source = source + "-recovery"
                        await submit(core, recovery_source, "recovery-goal",
                                     f"这是新的一次资料评估任务，与上次已经完成的报名不同。松风的候选页面是{url}，预算100元，只能周末参加。请只回复收到，不要现在调查；空闲时帮我核对。",
                                     metadata={"session_dimensions": {"project": report["project"]}})
                        await until(lambda: any("recovery-goal" in load_message_nodes(p)
                                                for p in (args.workspace / "memory").rglob("akasha.db")), timeout=180)
                        await settings({"context_session_id": recovery_source, "daily_budget": 3})
                        classes = {getattr(m, "Producer", None) for m in tuple(sys.modules.values())}
                        classes = {cls for cls in classes if isinstance(cls, type) and hasattr(cls, "_handoff")}

                        async def lost_ack(producer, key, value):
                            proposal = value["proposal"]
                            await run_file_io(lambda: producer.domain.propose(
                                proposal["proposal_id"], proposal["revision"], proposal["payload"],
                                datetime.fromisoformat(proposal["due_at"])))
                            raise RuntimeError("injected handoff acknowledgment loss")

                        with ExitStack() as stack:
                            for cls in classes:
                                stack.enter_context(patch.object(cls, "_handoff", lost_ack))
                            try:
                                await rpc(core, "drift/run", {})
                            except RuntimeError as error:
                                assert "injected handoff" in str(error), str(error)
                            else:
                                raise AssertionError("fault hook did not run inside the actual native producer")
                        before = metrics(args.workspace, model)["remote_chat_calls"]
                        planned = (await status())["runs"][0]
                        assert planned["status"] == "planned", planned
                        await core.plugin_manager.live_root.context.require(DRIFT_INITIATIVE).run_once()
                        replayed = (await status())["runs"][0]
                        assert replayed["status"] == "offered" and replayed["model_call_id"] == planned["model_call_id"]
                        assert metrics(args.workspace, model)["remote_chat_calls"] == before
                        snapshot = await core.plugin_manager.live_root.context.require(DRIFT_WAKE_V2).snapshot(datetime.now(UTC))
                        selected = [p for p in snapshot["proposals"] if p["ref"]["proposal_id"] == planned["proposal"]["proposal_id"]]
                        assert len(selected) == 1 and len(notifications()) == 1
                        report["fault_handoff"] = {"planned": planned, "replayed": replayed,
                                                  "matching_domain_proposals": selected, "new_model_calls_on_replay": 0}
                        report["before_restart"] = await status()
                        report["before_restart_calls"] = before
                        return {"actual_native_hook": True, "domain_proposals": 1, "new_model_calls_on_replay": 0}

                    await case("lost_handoff_ack_reuses_the_native_proposal_and_model_receipt", handoff_fault)
            report["metrics"] = metrics(args.workspace, model)
            report["http_fixture_requests"] = requests

        async with runtime(args.workspace) as core:
            async def restart():
                value = await rpc(core, "drift/status", {})
                assert {k: v for k, v in value.items() if k != "last_error"} == {
                    k: v for k, v in report["before_restart"].items() if k != "last_error"
                }
                assert metrics(args.workspace, model)["remote_chat_calls"] == report["before_restart_calls"]
                return {"state_unchanged": True, "new_model_calls": 0}
            await case("restart_preserves_receipts_and_does_not_repeat_planning", restart)
            if args.fault_cases and "fault_handoff" in report:
                async def resume_research():
                    await configure_proactive(core, args.workspace, inbox)

                    def deliveries():
                        context = core.plugin_manager.live_root.context
                        reader = context.require(MESSAGE_CATALOG).reader(inbox)
                        return [m for m in reader.snapshot() if isinstance(m.body, Output) and m.source == "wake"
                                and context.require(DELIVERY_READ).confirmed(m.message_id) is not None]
                    async with asyncio.timeout(240):
                        while True:
                            value = await rpc(core, "drift/status", {})
                            target_run = report["fault_handoff"]["planned"]["run_id"]
                            result = next(r for r in value["runs"] if r["run_id"] == target_run)
                            if result.get("domain_status") in {"settled", "await_change", "invalidated", "failed"}:
                                break
                            await asyncio.sleep(0.1)
                    assert result["domain_status"] in {"settled", "await_change"}, result
                    count = len(deliveries())
                    assert count == (2 if result["domain_status"] == "settled" else 1)
                    report["resumed_notification_ids"] = [m.message_id for m in deliveries()]
                    report["resumed_domain_status"] = result["domain_status"]
                    report["after_resume_metrics"] = metrics(args.workspace, model)
                    return {"domain_status": result["domain_status"], "total_confirmed_deliveries": count,
                            "resumed_proposal_deliveries": count - 1, "duplicate_deliveries": 0}
                await case("restart_then_resume_wake_settles_the_original_proposal_without_duplicate_delivery", resume_research)
        return all(r["passed"] for r in report["cases"])
    finally:
        server.close()
        await server.wait_closed()
        if "metrics" in report:
            report["final_metrics"] = metrics(args.workspace, model)
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "key-file", "embedding-manifest", "model-config", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--implicit-goal", action="store_true")
    parser.add_argument("--fault-cases", action="store_true")
    args = parser.parse_args()
    os.environ["AKASHIC_PLUGIN_HOME"] = str(args.workspace.parent / (args.workspace.name + "-home"))

    async def run():
        async with provider_slot(EvaluationModel.read(args.model_config), args.key_file):
            return await evaluate(args)
    raise SystemExit(0 if asyncio.run(run()) else 1)


if __name__ == "__main__":
    main()
