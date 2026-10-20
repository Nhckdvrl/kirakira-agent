"""Step5 Token Plan exercises through the formal production plugin graph."""
import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

from kirakira_agent.runtime import (
    ROOT,
    configure_step_plan,
    initialize_workspace,
    runtime,
    submit,
)
from plugins.ledger.contract import (
    MESSAGE_CATALOG,
    ContentPart,
    Input,
    Output,
    ToolCall,
    ToolResult,
)


def text(message):
    return "\n".join(str(part.value) for part in message.body.parts
                     if isinstance(part, ContentPart) and part.kind == "text")


async def evaluate(workspace, key_file, output):
    initialize_workspace(workspace)
    extensions = workspace.parent / (workspace.name + "-fixtures")
    shutil.copytree(ROOT / "eval/fixtures/native_extension", extensions / "native_extension",
                    ignore=shutil.ignore_patterns("__pycache__"))
    results = []
    nonce = uuid4().hex[:10]

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
        output.write_text(json.dumps({"billing_surface": "step_plan", "model": "step-5-preview",
                                      "workspace": str(workspace), "cases": results}, indent=2) + "\n")
        print(json.dumps({k: v for k, v in row.items() if k != "detail"}), flush=True)

    async with runtime(workspace, extensions=(extensions,)) as core:
        await configure_step_plan(core, key_file)
        root = core.plugin_manager.live_root
        catalog = root.context.require(MESSAGE_CATALOG)

        async def basic():
            answer = await submit(core, "akashic:basic", "basic", "Reply exactly BASIC_" + nonce + ". Do not use tools.")
            assert "BASIC_" + nonce in text(answer), text(answer)
            return {"answer": text(answer)}
        await record("native_reply", basic)

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
            before = core.plugin_manager.read_config_input("step-plan")["input_ref"]
            save_config(workspace / "plugin-data/step-plan-builtin", {"evaluation_revision": 1})
            await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"step-plan"}))
            after = core.plugin_manager.read_config_input("step-plan")["input_ref"]
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
                    match = next((row for row in reader.snapshot() if predicate(row)), None)
                    if match is not None:
                        return match
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
                f"Use write_file to create {path} containing exactly ONCE_{nonce}. After writing, report the content. Do not write the file twice."))
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
                assert path.read_text() == "ONCE_" + nonce
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
                    "model_id": "step5", "reasoning_effort": "medium"}}]})
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
                    "source": ending.source, "parent_reasoning_effort": "medium"}
        await record("scheduler_native_source_delivery", scheduler)

    async with runtime(workspace, extensions=(extensions,)) as core:
        async def restart():
            answer = await submit(core, "akashic:files", "files-restart", "Read evidence.txt from the current workspace and report the exact token stored there. Use read_file.")
            assert "FILE_" + nonce in text(answer)
            return {"answer": text(answer)}
        await record("restart_reuses_native_history", restart)
    with sqlite3.connect((workspace / "model-registry.sqlite3").as_uri() + "?mode=ro", uri=True) as database:
        calls = database.execute("SELECT binding_json,state,usage_json FROM model_calls").fetchall()
        endpoints = [row[0] for row in database.execute("SELECT base_url FROM model_connections")]
    from plugins.step_plan.driver import validate_endpoint
    assert endpoints and all(validate_endpoint(endpoint) for endpoint in endpoints)
    drivers = {json.loads(row[0])["driver_id"] for row in calls}
    assert drivers == {"step-plan"}, drivers
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
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("AKASHIC_PLUGIN_HOME", str(args.workspace.parent / (args.workspace.name + "-home")))
    results = asyncio.run(evaluate(args.workspace.resolve(), args.key_file, args.output))
    raise SystemExit(0 if all(row["passed"] for row in results) else 1)


if __name__ == "__main__":
    main()
