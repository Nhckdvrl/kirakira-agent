"""Real-provider exercises through the production plugin graph."""
import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from eval.models import EvaluationModel, source_identity
from kirakira_agent.runtime import (
    ROOT,
    initialize_workspace,
    runtime,
    submit,
)
from plugins.ledger.contract import (
    MESSAGE_CATALOG,
    ContentPart,
    Control,
    Input,
    Output,
    ToolCall,
    ToolResult,
)


def text(message):
    return "\n".join(str(part.value) for part in message.body.parts
                     if isinstance(part, ContentPart) and part.kind == "text")


def _successful_tool_calls(reader):
    """Resolve successful receipts through their actual call and archived tool binding."""
    rows = reader.snapshot()
    by_id = {row.message_id: row for row in rows}
    resolved = []
    for receipt in rows:
        if receipt.source != "conversation" or not isinstance(receipt.body, ToolResult) or receipt.body.outcome != "success":
            continue
        ref = receipt.body.call_ref
        caller = by_id.get(ref.message_id)
        assert caller is not None and caller.source == receipt.source and isinstance(caller.body, Output), "successful tool receipt lacks its actual caller"
        assert 0 <= ref.part_index < len(caller.body.parts), "tool receipt points outside its actual Output"
        call = caller.body.parts[ref.part_index]
        assert isinstance(call, ToolCall), "tool receipt does not point to a ToolCall"
        page = reader.read_page(after_seq=caller.seq - 1, through_seq=caller.seq, limit=1)
        name = page.bindings[call.binding_id]["metadata"]["tool"]["name"]
        resolved.append((receipt, call, name))
    return tuple(resolved)


def _search_evidence(reader, *, page_url, marker):
    """Require source facts from the successful native calls, not another tool's output."""
    resolved = _successful_tool_calls(reader)
    searches = [receipt for receipt, _, name in resolved if name == "web_search" and page_url in text(receipt)]
    fetches = [receipt for receipt, call, name in resolved
               if name == "web_fetch" and call.arguments.get("url") == page_url and marker in text(receipt)]
    assert searches, "missing successful web_search receipt containing the original page URL"
    assert fetches, "missing successful web_fetch receipt for the original page with its source marker"
    return {"successful_tool_receipts": len(searches) + len(fetches),
            "search_tool_receipts": len(searches), "fetch_tool_receipts": len(fetches)}


def _probe_evidence(reader, *, value):
    probe = [receipt for receipt, call, name in _successful_tool_calls(reader)
             if name == "native_probe" and call.arguments.get("value") == value and value in text(receipt)]
    assert len(probe) == 1, "missing the one successful native_probe receipt for the original LIMIT token"
    return len(probe)


@asynccontextmanager
async def search_source(nonce):
    """A local search protocol fixture; the answer exists only on the fetched page."""
    requests = {"search": 0, "fetch": 0}
    page_url = ""

    async def serve(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            location = urlsplit(header.decode("ascii").split(" ", 2)[1])
            if location.path == "/nested/search":
                assert parse_qs(location.query).get("format") == ["json"]
                requests["search"] += 1
                body = json.dumps({"results": [{"title": "个人助理研究活动：最新公布的时间",
                    "url": page_url, "content": "完整页面包含准确时间与来源校验码，请读取原文确认。"}]}).encode()
                content_type = b"application/json"
            else:
                assert location.path == "/announcement"
                requests["fetch"] += 1
                body = ("<html><meta charset='utf-8'><h1>活动公告</h1>"
                        "<p>公布的时间为 2026-11-03 09:20，时区 Asia/Tokyo。</p>"
                        f"<p>来源校验码 EVIDENCE_{nonce}</p></html>").encode()
                content_type = b"text/html; charset=utf-8"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type
                         + b"\r\nContent-Length: " + str(len(body)).encode()
                         + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/nested"
        page_url = endpoint.removesuffix("/nested") + "/announcement"
        yield endpoint, page_url, requests


async def evaluate(workspace, key_file, output, *, model_config=None):
    model_config = model_config or EvaluationModel.read(None)
    initialize_workspace(workspace)
    extensions = workspace.parent / (workspace.name + "-fixtures")
    shutil.copytree(ROOT / "eval/fixtures/native_extension", extensions / "native_extension",
                    ignore=shutil.ignore_patterns("__pycache__"))
    # Package data may lose executable bits. Restore the trusted fixture's
    # entry points before the plugin manager freezes its immutable artifact.
    # Their shebang uses the active environment selected by runtime().
    for name in ("health.py", "mcp_server.py"):
        entry = extensions / "native_extension" / name
        entry.chmod(entry.stat().st_mode | 0o100)
    results = []
    nonce = uuid4().hex[:10]
    sources = source_identity()

    async def record(name, operation):
        start = time.monotonic()
        try:
            detail = await operation()
            if "answer" in detail:
                assert "<tool_call>" not in detail["answer"]
            row = {"name": name, "passed": True, "detail": detail}
        except Exception as error:  # noqa: BLE001 - record the actual case failure and continue independent cases
            row = {"name": name, "passed": False, "error": type(error).__name__ + ": " + str(error)[:1500]}
        row["duration_s"] = round(time.monotonic() - start, 3)
        results.append(row)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"model_config": model_config.description(), "source_identity": sources,
                                      "workspace": str(workspace), "cases": results}, indent=2) + "\n")
        print(json.dumps({k: v for k, v in row.items() if k != "detail"}), flush=True)

    async with AsyncExitStack() as stack:
        from agent.plugin_composition.config_input import save_config

        endpoint, page_url, web_requests = await stack.enter_async_context(search_source(nonce))
        save_config(workspace / "plugin-data/web_search_searxng-builtin", {"endpoint": endpoint})
        save_config(workspace / "plugin-data/standard_web-builtin", {"search_provider": "web_search_searxng"})
        core = await stack.enter_async_context(runtime(workspace, extensions=(extensions,)))
        await model_config.configure(core, key_file)
        root = core.plugin_manager.live_root
        catalog = root.context.require(MESSAGE_CATALOG)

        async def basic():
            answer = await submit(core, "akashic:basic", "basic", "Reply exactly BASIC_" + nonce + ". Do not use tools.")
            assert "BASIC_" + nonce in text(answer), text(answer)
            return {"answer": text(answer)}
        await record("native_reply", basic)

        async def sourced_search():
            session = "akashic:search-source"
            baseline = dict(web_requests)
            answer = await submit(core, session, "search-source-1",
                "请用 web_search 搜索个人助理研究活动最新公布的时间，然后用 web_fetch 读取搜索结果中的原文。"
                "请报告原文的准确日期、时间、时区、来源校验码，并给出原文链接。不要凭摘要推测，也不要用 shell。")
            visible = text(answer)
            assert all(value in visible for value in ("2026-11-03", "09:20", "Asia/Tokyo", "EVIDENCE_" + nonce, page_url))
            evidence = _search_evidence(catalog.reader(session), page_url=page_url, marker="EVIDENCE_" + nonce)
            requests = {kind: web_requests[kind] - baseline[kind] for kind in baseline}
            assert requests["search"] >= 1 and requests["fetch"] >= 1
            return {"search_requests": requests["search"], "fetch_requests": requests["fetch"],
                    **evidence, "exact_page_facts": True,
                    "scope": "real model and native tools against a local SearXNG protocol fixture; not public engine quality"}
        await record("selected_search_fetch_source_evidence", sourced_search)

        async def file_roundtrip():
            path = workspace / "evidence.txt"
            answer = await submit(core, "akashic:files", "files-1", f"Use write_file to create {path} containing exactly FILE_{nonce}. Then use read_file to verify it. Report the token after verification. Do not simulate tools.")
            assert path.read_text() == "FILE_" + nonce
            assert "FILE_" + nonce in text(answer)
            rows = catalog.reader("akashic:files").snapshot()
            assert sum(isinstance(row.body, ToolResult) for row in rows) >= 2
            return {"answer": text(answer), "facts": len(rows)}
        await record("bound_file_roundtrip", file_roundtrip)

        async def mcp():
            answer = await submit(core, "akashic:mcp", "mcp-1", "Call native_probe with value MCP_" + nonce + ". Report the returned value and managed_pid. Do not use shell or simulate the result.")
            assert "MCP_" + nonce in text(answer)
            results_ = [row for row in catalog.reader("akashic:mcp").snapshot() if isinstance(row.body, ToolResult)]
            assert any("managed_pid" in str(row.body.parts) and row.body.outcome == "success" for row in results_)
            assert any('"python": [3, 14, 6]' in str(row.body.parts) for row in results_)
            return {"answer": text(answer), "results": len(results_)}
        await record("managed_process_mcp", mcp)

        async def shell():
            answer = await submit(core, "akashic:shell", "shell-1", "Use the shell tool with shell='bash', login=false and command='echo SHELL_" + nonce + "'. Report its actual stdout.")
            assert "SHELL_" + nonce in text(answer)
            assert any(isinstance(row.body, ToolResult) and "SHELL_" + nonce in str(row.body.parts)
                       for row in catalog.reader("akashic:shell").snapshot())
            return {"answer": text(answer)}
        await record("native_shell", shell)

        async def tool_repair():
            missing = workspace / ("missing-" + nonce)
            answer = await submit(core, "akashic:repair", "repair-1", f"Use read_file on {missing}. The path is intentionally absent. After observing the error, call native_probe with value REPAIRED_{nonce} and report its real response.")
            assert "REPAIRED_" + nonce in text(answer)
            rows = catalog.reader("akashic:repair").snapshot()
            assert sum(isinstance(row.body, ToolResult) for row in rows) >= 2
            assert any(isinstance(row.body, ToolResult) and row.body.outcome == "error" for row in rows)
            return {"answer": text(answer)}
        await record("tool_error_repair", tool_repair)

        async def child():
            answer = await submit(core, "akashic:child", "child-1", "Load the subagent tool group, then call spawn with profile='scripting', run_in_background=false and task='Reply exactly CHILD_" + nonce + ", without tools'. Report its completed result. You must actually call spawn.")
            assert "CHILD_" + nonce in text(answer)
            sessions = catalog.snapshot_heads()
            assert any(key.startswith("subagent:") for key in sessions)
            return {"answer": text(answer), "child_sessions": [key for key in sessions if key.startswith("subagent:")]}
        await record("native_subagent", child)

        async def model_reload():
            from agent.plugin_composition.config_input import save_config
            token = root.instance_token
            before = core.plugin_manager.read_config_input(model_config.driver_id)["input_ref"]
            save_config(workspace / ("plugin-data/" + model_config.driver_id + "-builtin"), {"evaluation_revision": 1})
            await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({model_config.driver_id}))
            after = core.plugin_manager.read_config_input(model_config.driver_id)["input_ref"]
            assert before != after
            assert core.plugin_manager.live_root.instance_token is token
            answer = await submit(core, "akashic:reload", "reload-1", "Reply exactly RELOADED_" + nonce + ". No tools.")
            assert "RELOADED_" + nonce in text(answer)
            return {"answer": text(answer), "same_root": True}
        await record("driver_local_reload", model_reload)

        async def wait_fact(session, predicate):
            reader = catalog.reader(session)
            async def wait():
                async for _ in reader.follow_heads():
                    rows = await reader.read_async(lambda current: current.snapshot())
                    match = next((row for row in rows if predicate(row)), None)
                    if match is not None:
                        return match
                    if failure := next((row for row in reversed(rows)
                                        if isinstance(row.body, Control) and row.body.action == "failure"), None):
                        status = re.search(r"HTTP\s*([1-5]\d{2})", failure.body.reason or "")
                        suffix = " (HTTP " + status[1] + ")" if status else ""
                        raise RuntimeError("native failure before awaited fact" + suffix)
            return await asyncio.wait_for(wait(), 90)

        async def steering():
            from datetime import UTC, datetime

            from plugins.channels.contract import CHANNEL_STEER, ChannelInboundMessage
            session = "akashic:steer"
            pending = asyncio.create_task(submit(core, session, "steer-1",
                "Call native_probe with value ORIGINAL_" + nonce + " and delay=3. Then report its response."))
            try:
                await wait_fact(session, lambda row: isinstance(row.body, Output) and any(isinstance(p, ToolCall) for p in row.body.parts))
                def message(value):
                    return ChannelInboundMessage("akashic", "user", "steer", value, datetime.now(UTC), {})
                with root.context.borrow(CHANNEL_STEER) as steers:
                    assert await steers.stage(session, "withdrawn", message("WRONG_TOKEN"))
                    assert steers.withdraw(session, "withdrawn")
                    assert await steers.stage(session, "steer-correction", message("Correction: after receiving the result, final answer must include STEER_" + nonce + "."))
                answer = await pending
                assert "STEER_" + nonce in text(answer), text(answer)
                rows = catalog.reader(session).snapshot()
                assert sum(isinstance(row.body, Input) for row in rows) == 2
                assert all(row.message_id != "withdrawn" for row in rows)
                return {"answer": text(answer), "accepted_inputs": 2}
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await record("task_bound_steer_withdraw", steering)

        async def resume_known_effect():
            from datetime import UTC, datetime

            from plugins.channels.contract import (
                CHANNEL_INPUT_V2,
                ChannelInboundMessage,
            )
            session = "akashic:resume"
            path = workspace / "resume-proof.txt"
            pending = asyncio.create_task(submit(core, session, "resume-input",
                f"Use write_file to create {path} containing exactly ONCE_{nonce}, with no trailing newline or other characters. After writing, report the content. Do not write the file twice."))
            try:
                await wait_fact(session, lambda row: isinstance(row.body, ToolResult) and row.body.outcome == "success")
                await root.context.require(CHANNEL_INPUT_V2)(session, "resume-stop",
                    ChannelInboundMessage("akashic", "user", "resume", "/stop", datetime.now(UTC), {}))
                await asyncio.gather(pending, return_exceptions=True)
                before = catalog.reader(session).snapshot()
                if any(isinstance(row.body, Output) and row.body.finish == "complete" for row in before):
                    raise AssertionError("reply completed before pause; interruption was not exercised")
                answer = await submit(core, session, "resume-control", "",
                                      metadata={"retry_of_client_message_id": "latest"})
                rows = catalog.reader(session).snapshot()
                assert "ONCE_" + nonce in text(answer)
                assert path.read_text() == "ONCE_" + nonce, "file content differs from the exact requested bytes: " + repr(path.read_text())
                bindings = catalog.reader(session).read_page(limit=100).bindings
                writes = [(row.message_id, index) for row in rows if isinstance(row.body, Output)
                          for index, part in enumerate(row.body.parts)
                          if isinstance(part, ToolCall)
                          and bindings[part.binding_id]["metadata"]["tool"]["name"] == "write_file"]
                assert len(writes) == 1, "completed write effect was proposed again"
                assert sum(isinstance(row.body, ToolResult)
                           and (row.body.call_ref.message_id, row.body.call_ref.part_index) == writes[0]
                           for row in rows) == 1
                return {"answer": text(answer), "write_effects": 1,
                        "tool_results": sum(isinstance(row.body, ToolResult) for row in rows)}
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await record("source_pause_resume_no_effect_replay", resume_known_effect)

        async def supersession():
            session = "akashic:supersede"
            first = asyncio.create_task(submit(core, session, "supersede-first",
                "Call native_probe with value FIRST_" + nonce + " and delay=3, then report it."))
            try:
                await wait_fact(session, lambda row: isinstance(row.body, Output) and any(isinstance(p, ToolCall) for p in row.body.parts))
                answer = await submit(core, session, "supersede-second",
                    "New instruction: use the already pending probe result; do not call it again. Final answer must include SUPERSEDE_" + nonce + ".")
                assert "SUPERSEDE_" + nonce in text(answer), text(answer)
                rows = catalog.reader(session).snapshot()
                assert sum(isinstance(row.body, ToolResult) for row in rows) == 1
                assert sum(isinstance(row.body, Input) for row in rows) == 2
                return {"answer": text(answer), "effect_results": 1}
            finally:
                if not first.done():
                    first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        await record("new_input_revokes_old_source_task", supersession)

        async def attachment():
            from plugins.ledger.contract import ARTIFACT_IMPORT, AttachmentKind
            path = workspace / "attached.txt"
            path.write_text("ATTACHMENT_" + nonce)
            with root.context.borrow(ARTIFACT_IMPORT) as importer:
                ref = await importer.import_source(str(path), AttachmentKind.FILE)
            answer = await submit(core, "akashic:attachment", "attachment-1",
                "Read the attached text file and report the exact token inside. Do not use tools; the attachment content is provided.", attachments=(ref,))
            assert "ATTACHMENT_" + nonce in text(answer), text(answer)
            rows = catalog.reader("akashic:attachment").snapshot()
            assert any(isinstance(row.body, Input) and any(part.kind == "artifact_ref" for part in row.body.parts) for row in rows)
            return {"answer": text(answer), "artifact_id": ref.artifact_id}
        await record("immutable_attachment_projection", attachment)

        async def scheduler():
            from plugins.scheduler.store import JobStore
            session = "akashic:scheduler"
            await submit(core, session, "scheduler-request",
                "Create exactly ONE scheduled task. Load the scheduler tool group once, then call schedule exactly once: action='run', trigger='after', when='2s', "
                "timezone='Asia/Tokyo', context='none', name='native-timer', "
                "text='Reply exactly TIMER_" + nonce + ", without tools'. Once the tool confirms registration, "
                "do not call load_tools or schedule again; acknowledge once and finish.",
                metadata={"input_parts": [{"kind": "model.selection", "value": {
                    "model_id": model_config.model_id, "reasoning_effort": "medium" if model_config.reasoning_effort is not None else None}}]})
            ending = await wait_fact(session, lambda row: row.source.startswith("scheduler:")
                and isinstance(row.body, Output) and row.body.finish == "complete")
            assert "TIMER_" + nonce in text(ending), text(ending)
            store = JobStore(workspace / "schedules.json")
            created = len(store.read().jobs)
            assert created == 1, f"model created {created} jobs for a single-task request"
            async def delivered():
                for _ in range(100):
                    fires = list(store.read().fires.values())
                    if fires and all(fire.status == "delivered" for fire in fires):
                        return len(fires)
                    await asyncio.sleep(0.05)
                raise AssertionError("scheduler delivery receipt did not settle")
            count = await delivered()
            return {"answer": text(ending), "created_jobs": created, "delivered_fires": count,
                    "source": ending.source, "parent_reasoning_effort": "medium" if model_config.reasoning_effort is not None else None}
        await record("scheduler_native_source_delivery", scheduler)

        async def exhaustion_recovery():
            session = "akashic:exhaustion"
            save_config(workspace / "plugin-data/reply-builtin", {"max_steps": 1})
            await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"reply"}))
            try:
                try:
                    await submit(core, session, "exhaustion-1",
                        "Call native_probe with value LIMIT_" + nonce
                        + ". You must actually call it and then report its returned value. Do not simulate tools.", timeout=90)
                except RuntimeError as error:
                    assert "abandon" in str(error), type(error).__name__
                else:
                    raise AssertionError("the one-step tool turn did not exercise exhaustion")
                before = catalog.reader(session).snapshot()
                probe_count = _probe_evidence(catalog.reader(session), value="LIMIT_" + nonce)
                assert any(isinstance(row.body, Control) and row.body.action == "abandon" for row in before)
                answer = await submit(core, session, "exhaustion-fresh",
                    "A fresh question: reply exactly FRESH_" + nonce + ". Do not use tools.")
                assert "FRESH_" + nonce in text(answer)
                after = catalog.reader(session).snapshot()
                assert after[:len(before)] == before
                assert sum(isinstance(row.body, ToolResult) for row in after) == 1
                return {"closed_by_abandon": True, "fresh_input_answered": True,
                        "original_receipts_preserved": True, "tool_results": 1,
                        "native_probe_receipts": probe_count, "probe_value_verified": True}
            finally:
                save_config(workspace / "plugin-data/reply-builtin", {})
                await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"reply"}))
        await record("step_limit_next_input_recovery", exhaustion_recovery)

    async with runtime(workspace, extensions=(extensions,)) as core:
        async def restart():
            answer = await submit(core, "akashic:files", "files-restart", "Read evidence.txt from the current workspace and report the exact token stored there. Use read_file.")
            assert "FILE_" + nonce in text(answer)
            return {"answer": text(answer)}
        await record("restart_reuses_native_history", restart)
    with sqlite3.connect((workspace / "model-registry.sqlite3").as_uri() + "?mode=ro", uri=True) as database:
        calls = database.execute("SELECT binding_json,state,usage_json FROM model_calls").fetchall()
        endpoints = [row[0] for row in database.execute("SELECT base_url FROM model_connections")]
    assert endpoints and all(endpoint == model_config.endpoint for endpoint in endpoints)
    drivers = {json.loads(row[0])["driver_id"] for row in calls}
    assert drivers == {model_config.driver_id}, drivers
    usages = [json.loads(row[2]) for row in calls if row[2] is not None]
    report = json.loads(output.read_text())
    report["summary"] = {"passed": sum(row["passed"] for row in results), "total": len(results),
        "python": sys.version.split()[0], "recorded_business_model_calls": len(calls),
        "model_states": {state: sum(row[1] == state for row in calls) for state in {row[1] for row in calls}},
        "driver_ids": sorted(drivers), "endpoints": endpoints,
        "input_tokens": sum(value.get("input_tokens") or 0 for value in usages),
        "output_tokens": sum(value.get("output_tokens") or 0 for value in usages),
        "coverage": "native Models business-call ledger; connection/model setup probes are additional calls"}
    output.write_text(json.dumps(report, indent=2) + "\n")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("AKASHIC_PLUGIN_HOME", str(args.workspace.parent / (args.workspace.name + "-home")))
    results = asyncio.run(evaluate(args.workspace.resolve(), args.key_file, args.output, model_config=EvaluationModel.read(args.model_config)))
    raise SystemExit(0 if all(row["passed"] for row in results) else 1)


if __name__ == "__main__":
    main()
