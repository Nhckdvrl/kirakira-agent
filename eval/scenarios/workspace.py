"""Functional Project/cwd tests using the actual runtime and Step5 Token Plan."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from bootstrap.runtime import build_runtime
from infra.providers.step_plan import STEP5_MODEL, STEP_PLAN_BASE_URL, StepPlanClient
from eval.scenarios.core import ObservedClient


async def verify(args):
    args.workspace.mkdir(parents=True, exist_ok=False)
    if args.output.exists():
        raise ValueError("choose a fresh output")
    secret = args.key_file.read_text().strip()
    for name in (
        "LIGHT_MODEL_ID",
        "LIGHT_OPENAI_COMPATIBLE_BASE_URL",
        "LIGHT_OPENAI_COMPATIBLE_API_KEY",
    ):
        os.environ.pop(name, None)
    os.environ.update(
        MODEL_ID=STEP5_MODEL,
        OPENAI_COMPATIBLE_BASE_URL=STEP_PLAN_BASE_URL,
        OPENAI_COMPATIBLE_API_KEY=secret,
        AGENT_STREAM="true",
    )
    client = StepPlanClient(api_key=secret, reasoning_effort="medium", timeout=120)
    report = {
        "endpoint": STEP_PLAN_BASE_URL,
        "model": STEP5_MODEL,
        "reasoning_effort": "medium",
        "purpose": "functional parity only",
        "billing_surface": "step_plan",
        "cases": [],
        "source_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                *Path("plugins/projects").glob("*.py"),
                *Path("plugins/working_directory").glob("*.py"),
                Path("bootstrap/workspace.py"),
                Path("session/owner_store.py"),
                Path("session/manager.py"),
                Path("agent/core/reasoner.py"),
                Path("agent/core/tool_io.py"),
                Path("agent/core/pipeline.py"),
                Path("agent/control/service.py"),
                Path("agent/control/protocol/models.py"),
                Path("agent/tools/builtins.py"),
                Path("agent/tools/registry.py"),
                Path("agent/model_runtime/context_compaction.py"),
                Path("agent/subagent.py"),
                Path("bootstrap/runtime.py"),
                Path("bootstrap/control.py"),
                Path("bootstrap/tools.py"),
                Path("infra/channels/web_chat_channel.py"),
                Path("frontend/web_ui.py"),
                Path("frontend/workspace_ui.py"),
                Path("eval/scenarios/workspace.py"),
            ]
        },
    }
    semaphore = asyncio.Semaphore(args.concurrency)

    def save():
        report["passed"] = sum(r["status"] == "passed" for r in report["cases"])
        report["total"] = len(report["cases"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def completed_tools(runtime):
        events = runtime.reasoner.execution_store.events(
            runtime.reasoner.execution_store.recent()[0]["run_id"]
        )
        intents = {
            e["payload"]["operation_id"]: e["payload"]
            for e in events
            if e["kind"] == "tool_intent"
        }
        return [
            {
                "name": intents[e["payload"]["operation_id"]]["name"],
                "arguments": intents[e["payload"]["operation_id"]]["arguments"],
                "is_error": e["payload"]["is_error"],
                "content": e["payload"]["content"],
            }
            for e in events
            if e["kind"] == "tool_result"
        ]

    async def rig(work, observed):
        work.mkdir(exist_ok=True)
        (work / "config.toml").write_text(
            '[llm.main]\nmodel="step-5-preview"\nbase_url="https://api.stepfun.com/step_plan/v1"\nreasoning_effort="medium"\n[agent]\nmax_iterations=16\nmax_tokens=8192\n[memory]\nenabled=false\n'
        )
        runtime = await build_runtime(work)
        runtime.reasoner._model_io.model_client = observed
        runtime.reasoner.model_client = observed
        for name in runtime.tools.names():
            if name not in {
                "set_working_directory",
                "read_file",
                "write_file",
                "list_dir",
                "bash",
                "write_stdin",
                "task_output",
                "task_stop",
            }:
                runtime.tools.unregister(name)
        return runtime

    async def case(kind, repeat):
        async with semaphore:
            observed = ObservedClient(client)
            work = args.workspace / f"{kind}-{repeat}"
            work.mkdir()
            a, b = work / "repo-a", work / "repo-b"
            a.mkdir()
            b.mkdir()
            (a / "AGENTS.md").write_text(
                "Repository marker: RULE_A_924. Verify written files by reading them."
            )
            (b / "AGENTS.md").write_text(
                "Repository marker: RULE_B_825. Verify written files by reading them."
            )
            nonce = "KIRA_LITERAL_" + uuid4().hex[:12]
            (b / "value.txt").write_text(nonce)
            runtime = await rig(work / "runtime", observed)
            row = {"kind": kind, "repeat": repeat}
            try:
                if kind == "project":
                    project = "p_" + uuid4().hex
                    runtime.control_service.workspace_call(
                        "project/create",
                        {
                            "projectId": project,
                            "name": "Native project",
                            "directory": str(a),
                        },
                    )
                    key = runtime.control_service.start_thread({"project_id": project})[
                        "id"
                    ]
                    prompt = f'Use write_file to write the exact literal string "{nonce}" into created.txt, then read_file to verify it. Do not change the working directory.'
                    result = await runtime.process_direct(
                        prompt,
                        session_key=key,
                        skip_memory_retrieval=True,
                        skip_post_memory=True,
                    )
                    assert (a / "created.txt").read_text().strip() == nonce, (
                        result.content
                    )
                    assert not (work / "runtime/created.txt").exists()
                    row["tool_results"] = completed_tools(runtime)
                    successful = {
                        t["name"] for t in row["tool_results"] if not t["is_error"]
                    }
                    assert {"write_file", "read_file"} <= successful
                    second = runtime.control_service.start_thread(
                        {"project_id": project}
                    )["id"]
                    assert runtime.working_directories.snapshot(second).path == str(a)
                    row["checks"] = {
                        "created_file": str(a / "created.txt"),
                        "exact_file_content": nonce,
                        "independent_session_default": str(a),
                    }
                    runtime.working_directories.switch(
                        key, str(b), receipt_key="before-delete"
                    )
                    runtime.control_service.delete_thread(key)
                    assert runtime.working_directories.snapshot(key).revision is None
                    runtime.session_manager.get_or_create(
                        key, metadata={"project_id": project}
                    )
                    assert runtime.working_directories.snapshot(key).path == str(a)
                    recreated = await runtime.process_direct(
                        "Read relative path created.txt and report its exact contents.",
                        session_key=key,
                        skip_memory_retrieval=True,
                        skip_post_memory=True,
                    )
                    row["delete_recreate_tool_results"] = completed_tools(runtime)
                    assert any(
                        t["name"] == "read_file"
                        and not t["is_error"]
                        and nonce in t["content"]
                        for t in row["delete_recreate_tool_results"]
                    )
                    assert nonce in recreated.content
                    row["checks"]["deleted_session_recreated_default"] = str(a)
                else:
                    key = "verify:directory"
                    runtime.session_manager.get_or_create(key)
                    runtime.working_directories.switch(key, str(a), receipt_key="setup")
                    result = await runtime.process_direct(
                        f'First switch the working directory to "{b}" by calling set_working_directory ALONE in one response. After it succeeds, read relative path value.txt and run bash with pwd (login=false). Report the exact file contents and directory.',
                        session_key=key,
                        skip_memory_retrieval=True,
                        skip_post_memory=True,
                    )
                    assert runtime.working_directories.snapshot(key).path == str(b), (
                        result.content
                    )
                    assert nonce in result.content and str(b) in result.content, (
                        result.content
                    )
                    events = runtime.reasoner.execution_store.events(
                        runtime.reasoner.execution_store.recent()[0]["run_id"]
                    )
                    systems = [
                        str(e["payload"]["messages"][0].get("content"))
                        for e in events
                        if e["kind"] == "model_request"
                    ]
                    assert any("RULE_A_924" in s for s in systems)
                    assert any(
                        "RULE_B_825" in s and "RULE_A_924" not in s for s in systems
                    )
                    row["tool_results"] = completed_tools(runtime)
                    successful = {
                        t["name"] for t in row["tool_results"] if not t["is_error"]
                    }
                    assert {"set_working_directory", "read_file", "bash"} <= successful
                    assert any(
                        t["name"] == "bash"
                        and not t["is_error"]
                        and str(b) in t["content"]
                        for t in row["tool_results"]
                    )
                    row["checks"] = {
                        "actual_directory": runtime.working_directories.snapshot(
                            key
                        ).path,
                        "exact_relative_file_content": nonce,
                        "old_rule_seen": True,
                        "new_request_replaced_old_rule": True,
                    }
                assert (
                    runtime.reasoner.execution_store.recent()[0]["status"]
                    == "completed"
                )
                row["reply"] = result.content
                if kind == "restart":
                    await runtime.stop_background([])
                    runtime = await rig(work / "runtime", observed)
                    assert runtime.working_directories.snapshot(key).path == str(b)
                    result = await runtime.process_direct(
                        "Read relative path value.txt again and report its exact contents.",
                        session_key=key,
                        skip_memory_retrieval=True,
                        skip_post_memory=True,
                    )
                    assert nonce in result.content, result.content
                    row["restart_reply"] = result.content
                    row["restart_tool_results"] = completed_tools(runtime)
                    assert any(
                        t["name"] == "read_file"
                        and not t["is_error"]
                        and nonce in t["content"]
                        for t in row["restart_tool_results"]
                    )
                    row["checks"]["persisted_directory_after_restart"] = str(b)
                row["status"] = "passed"
            except Exception as exc:
                row.update(
                    status="failed",
                    error=type(exc).__name__,
                    detail=str(exc).replace(secret, "[redacted]")[:1000],
                )
            finally:
                await runtime.stop_background([])
                row["requests"] = observed.requests
                report["cases"].append(row)
                save()
                print(
                    json.dumps({k: row[k] for k in ("kind", "repeat", "status")}),
                    flush=True,
                )

    await asyncio.gather(
        *(
            case(kind, repeat)
            for repeat in range(args.repeats)
            for kind in ("directory", "project", "restart")
        )
    )
    save()
    if report["passed"] != report["total"]:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1 or args.concurrency < 1:
        parser.error("repeats and concurrency must be positive")
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
