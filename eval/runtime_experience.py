"""Measure local service experience, source continuity and native I/O.

Service exercises use internal sessions excluded from memory learning. Browser
exercises require a fresh isolated workspace. Reports contain timings and counts,
never conversation bodies, model reasoning or credentials.
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, closing, suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from kirakira_agent.client import AsyncClient


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def process_snapshot(workspace: Path) -> dict:
    """Linux process counters; no command line, environment or host identity."""
    try:
        pid = json.loads((workspace / ".runtime-ready.json").read_text())["pid"]
        root = Path("/proc") / str(pid)
        # The comm field is parenthesized and may contain spaces.
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines() if ":" in line)
        return {"cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                "rss_kib": int(status["VmRSS"].split()[0]), "swap_kib": int(status["VmSwap"].split()[0])}
    except (OSError, KeyError, ValueError, IndexError):
        return {}


def visible_preview(value: object) -> bool:
    if isinstance(value, dict):
        preview = value.get("preview")
        if value.get("active", True) and isinstance(preview, dict) and preview.get("text"):
            return True
        return any(visible_preview(v) for key, v in value.items() if key not in {"thinking", "body", "display"})
    return isinstance(value, list) and any(visible_preview(item) for item in value)


async def measure_turn(client, session: str, text: str, *, programmatic=False, parts=None,
                       expected_source: str | None = None) -> dict:
    """Observe an actual durable ACK, public preview and complete output."""
    head = (await client.message_read(session, limit=1))["through_seq"]
    result = {"passed": False, "tool_results": 0, "first_visible_ms": None, "after_seq": head}
    identity = uuid4().hex
    current_input, input_seq = None, None
    if expected_source is not None:
        result["input_message_id"] = identity
    async with await client.session_follow(session, after_seq=head) as feed:
        begin = time.monotonic()
        if programmatic:
            await client.request("programmatic/message/send", {
                "session_id": session, "message_id": identity, "text": text})
        else:
            await client.message_send(session, text, message_id=identity, parts=parts)
        result["ack_ms"] = (time.monotonic() - begin) * 1000
        async with asyncio.timeout(150):
            async for event in feed.events():
                preview = event if expected_source is None else [item for item in event.get("items", [])
                                                                 if item.get("source") == expected_source]
                if (result["first_visible_ms"] is None and event.get("type") == "reply.status"
                    and (expected_source is None or current_input == identity) and visible_preview(preview)):
                    result["first_visible_ms"] = (time.monotonic() - begin) * 1000
                for message in event.get("items", []):
                    body = message.get("body", {})
                    if expected_source is not None:
                        if message.get("source") != expected_source:
                            continue
                        if body.get("kind") == "input" and message.get("seq", -1) > head:
                            current_input = message.get("id")
                            if current_input == identity:
                                input_seq = message["seq"]
                        if current_input != identity or input_seq is None or message.get("seq", -1) <= input_seq:
                            continue
                    result["tool_results"] += body.get("kind") == "tool_result"
                    if body.get("kind") == "control" and body.get("action") == "failure":
                        if expected_source is not None and body.get("through_seq", -1) < input_seq:
                            continue
                        result.update(failure="native failure control", total_ms=(time.monotonic() - begin) * 1000,
                                      through_seq=message["seq"])
                        return result
                    if body.get("kind") == "output" and body.get("finish") == "complete":
                        result.update(passed=True, total_ms=(time.monotonic() - begin) * 1000,
                                      through_seq=message["seq"], ending_message_id=message["id"])
                        return result
    raise ConnectionError("reply subscription ended without a terminal result")


def call_metrics(workspace: Path, identities: list[str]) -> list[dict]:
    result = []
    with closing(sqlite3.connect((workspace.resolve() / "model-registry.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        for identity in dict.fromkeys(identities):
            row = db.execute("SELECT duration_ms,first_token_ms,usage_json,state FROM model_calls WHERE id=?", (identity,)).fetchone()
            if row:
                result.append({"duration_ms": row[0], "first_token_ms": row[1],
                               "usage": json.loads(row[2]) if row[2] else None, "state": row[3]})
    return result


def failure_http_statuses(workspace: Path, session: str, *, after_seq=-1, through_seq=None) -> list[int]:
    """Export only HTTP status numbers from this synthetic turn's Control failures."""
    statuses = set()
    uri = (workspace.resolve() / "sessions.db").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        for encoded, in db.execute(
            "SELECT body FROM messages WHERE session_key=? AND seq>? AND (? IS NULL OR seq<=?) ORDER BY seq",
            (session, after_seq, through_seq, through_seq),
        ):
            body = json.loads(encoded)
            if body.get("kind") == "control" and body.get("action") == "failure":
                reason = body.get("reason")
                if isinstance(reason, str):
                    statuses.update(int(value) for value in re.findall(r"\bHTTP\s*([1-5]\d{2})\b", reason))
    return sorted(statuses)


def turn_model_metrics(workspace: Path, session: str, row: dict) -> list[dict]:
    # Public UI rows deliberately hide internal parts without their owner.
    # Observe only this synthetic turn's canonical call identities locally.
    identities = []
    row["failure_http_statuses"] = failure_http_statuses(
        workspace, session, after_seq=row["after_seq"], through_seq=row["through_seq"])
    with closing(sqlite3.connect((workspace.resolve() / "sessions.db").as_uri() + "?mode=ro", uri=True)) as db:
        for body, in db.execute("SELECT body FROM messages WHERE session_key=? AND seq>? AND seq<=? ORDER BY seq",
                                (session, row.pop("after_seq"), row.pop("through_seq"))):
            for part in json.loads(body).get("parts", []):
                if part.get("kind") == "model.facts":
                    identities.append(part["value"]["call_record_id"])
    return call_metrics(workspace, identities)


async def service(args) -> dict:
    workspace = args.workspace.resolve()
    report = {"scope": "Existing local service. Synthetic exercises are internal and excluded from memory learning. Model first_token_ms may include reasoning; first_visible_ms observes only public text.",
              "started_at": datetime.now(UTC).isoformat(), "read_only": [], "cases": []}
    sessions = {}
    report["process_before"] = process_snapshot(workspace)
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(workspace / "runtime/web-chat.sock")), base_url="http://local", timeout=20) as http:
        for route in ("/api/chat/health", "/api/chat/sessions", "/api/chat/web-ui/bootstrap"):
            for repetition in range(args.samples):
                begin = time.monotonic()
                response = await http.get(route)
                report["read_only"].append({"route": route, "repetition": repetition + 1,
                    "duration_ms": (time.monotonic() - begin) * 1000,
                    "status": response.status_code, "bytes": len(response.content)})
    if args.exercise:
        async with await AsyncClient.for_workspace(workspace) as client:
            for label, prompt in (("sdk-greeting", "你好"), ("sdk-available", "在吗"),
                                  ("sdk-arithmetic", "37+48是多少？"), ("sdk-identity", "你现在用的是什么模型？")):
                session = "programmatic:experience-" + uuid4().hex
                sessions[session] = label
                await client.request("programmatic/session/admit", {"session_id": session, "persist_memory": False})
                before = process_snapshot(workspace)
                row = await measure_turn(client, session, prompt, programmatic=True)
                after = process_snapshot(workspace)
                if before and after:
                    row["worker_cpu_seconds"] = after["cpu_seconds"] - before["cpu_seconds"]
                row["model_calls"] = turn_model_metrics(workspace, session, row)
                row.pop("ending_message_id", None)
                row["case"] = label
                report["cases"].append(row)
                write_report(args.output, report)
        for number, prompt in enumerate(("你好", "37+48是多少？")):
            session = "programmatic:experience-cli-" + uuid4().hex
            sessions[session] = "cli-" + str(number + 1)
            begin = time.monotonic()
            process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "kirakira_agent", "exec",
                "--workspace", str(workspace), "--new", "--session", session, "--json", prompt,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(150):
                    stdout, _stderr = await process.communicate()
            except BaseException:
                process.terminate()
                await process.wait()
                raise
            report["cases"].append({"case": sessions[session], "passed": process.returncode == 0,
                "total_ms": (time.monotonic() - begin) * 1000, "exit_code": process.returncode,
                "output_bytes": len(stdout), "failure_http_statuses": failure_http_statuses(workspace, session)})
    # Optional journal attribution never exports raw log messages or other sessions.
    report["phases"] = {label: [] for label in sessions.values()}
    try:
        since = datetime.fromisoformat(report["started_at"]).strftime("%Y-%m-%d %H:%M:%S UTC")
        raw = subprocess.check_output(["journalctl", "--user", "-u", "kirakira-agent.service", "--since", since,
                                       "-o", "json", "--no-pager"], text=True, stderr=subprocess.DEVNULL)
        for line in raw.splitlines():
            try:
                row = json.loads(json.loads(line)["MESSAGE"])
            except (ValueError, KeyError, TypeError):
                continue
            label = sessions.get(row.get("session_id"))
            if label and row.get("event") == "runtime.timing":
                report["phases"][label].append({k: row[k] for k in ("phase", "operation_id", "measurement_value", "counts") if k in row})
    except (OSError, subprocess.CalledProcessError):
        report["journal"] = "unavailable; client and model ledger timings remain valid"
    report["process_after"] = process_snapshot(workspace)
    return report


async def references(args) -> dict:
    """Actual model, SDK input parts and live bounded source reads."""
    from eval.models import EvaluationModel, source_identity
    from eval.native_runtime import text
    from kirakira_agent.runtime import ROOT, initialize_workspace, rpc, runtime
    from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, Output, ToolCall
    from plugins.tools.contract import TOOLS

    if args.workspace.exists():
        raise ValueError("reference exercises require a new isolated workspace")
    os.environ["AKASHIC_PLUGIN_HOME"] = str(args.workspace / "isolated-plugin-home")
    initialize_workspace(args.workspace)
    extension = args.workspace / "fixtures"
    shutil.copytree(ROOT / "eval/fixtures/alignment", extension / "alignment", ignore=shutil.ignore_patterns("__pycache__"))
    nonce, updated = uuid4().hex[:12], uuid4().hex[:12]
    source = "项目 KIRA-LIVE 的历史资料。\n" + "这是背景记录，没有新决定。\n" * 450
    source += f"\n最后确认：预算760元，截止11月8日，校验码 {nonce}。"
    target, current = "akashic:source-" + uuid4().hex, "akashic:current-" + uuid4().hex
    parts = [{"kind": "session.reference", "value": {"sessions": [{"session_id": target, "title": "项目原记录"}]}}]
    report = {"scope": "Synthetic long conversation and later correction through real SDK, native tools and configured model. Not an open-ended memory benchmark.",
              "implementation": source_identity(), "model_config": EvaluationModel.read(args.model_config).description(),
              "source_chars": len(source), "reference_bytes": len(json.dumps(parts, ensure_ascii=False).encode()), "cases": []}
    write_report(args.output, report)
    async with runtime(args.workspace, extensions=(extension,)) as core:
        await EvaluationModel.read(args.model_config).configure(core, args.key_file)
        await rpc(core, "evaluation/seed", {"session_id": target, "visibility": "internal", "records": [
            {"kind": "input", "text": source}, {"kind": "output", "text": "已记录。"}]})
        await rpc(core, "evaluation/seed", {"session_id": current, "visibility": "internal"})
        context = core.plugin_manager.live_root.context
        catalog, bindings = context.require(MESSAGE_CATALOG), context.require(BINDINGS)
        original = catalog.reader(target).snapshot()
        async with await AsyncClient.for_workspace(args.workspace) as client:
            async def record(label, session, prompt, *, input_parts=None, expected=()):
                head = catalog.reader(session).head()
                row = await measure_turn(client, session, prompt, parts=input_parts)
                answer = catalog.reader(session).get(row.pop("ending_message_id", ""))
                content = text(answer) if answer is not None else ""
                calls = [part for message in catalog.reader(session).snapshot(after_seq=head)
                         if isinstance(message.body, Output)
                         for part in getattr(message.body, "parts", ()) if isinstance(part, ToolCall)]
                row.update(case=label, expected_facts_present=all(value in content for value in expected),
                           tools=[bindings.describe(call.binding_id, TOOLS)["tool"]["name"] for call in calls],
                           source_window_offsets=[call.arguments["offset"] for call in calls
                               if "message_id" in call.arguments and "offset" in call.arguments])
                row["model_calls"] = turn_model_metrics(args.workspace, session, row)
                row["passed"] = row["passed"] and row["expected_facts_present"]
                if label != "source-update":
                    row["passed"] = row["passed"] and "read_session" in row["tools"]
                report["cases"].append(row)
                write_report(args.output, report)

            await record("long-source", current,
                "引用会话里项目最终确认的预算、截止日期、校验码分别是什么？请用read_session按需展开原文，核对后简答。",
                input_parts=parts, expected=("760", "11月8", nonce))
            await record("source-update", target,
                f"更新最后决定：预算改为900元，截止改为11月12日，校验码改为{updated}。仅确认收到，不调用工具。")
            await record("live-correction", current,
                "刚才引用的原会话更新了决定。重新调用read_session核对，现在的预算、截止日期、校验码是什么？简答。",
                expected=("900", "11月12", updated))
        report["original_source_prefix_unchanged"] = catalog.reader(target).snapshot()[:len(original)] == original
        report["long_source_expanded"] = any(offset >= 4000 for offset in report["cases"][0]["source_window_offsets"])
        report["passed"] = all(row["passed"] for row in report["cases"]) and report["original_source_prefix_unchanged"] and report["long_source_expanded"]
    report["shutdown_completed"] = True
    return report


@asynccontextmanager
async def background_source(nonce):
    """A real HTTP document withheld until ordinary conversation is checked."""
    gate, requested = asyncio.Event(), asyncio.Event()
    state = {"requests": 0, "served": 0, "gate": gate, "requested": requested,
             "first_requested_at": None, "longest_wait_ms": 0.0, "fixture_timeouts": 0,
             "cleanup_completed": False}
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            path = header.split(b"\r\n", 1)[0].split(b" ")[1]
            if path == b"/project":
                state["requests"] += 1
                begin = time.monotonic()
                if state["first_requested_at"] is None:
                    state["first_requested_at"] = begin
                requested.set()
                try:
                    await asyncio.wait_for(gate.wait(), 120)
                except TimeoutError:
                    state["fixture_timeouts"] += 1
                    return
                finally:
                    state["longest_wait_ms"] = max(state["longest_wait_ms"], (time.monotonic() - begin) * 1000)
                body = ("<html><head><meta charset='utf-8'></head><body><h1>青禾项目公告</h1>"
                        f"<p>资料核验码：{nonce}。申请截止：2026年11月18日18:00（Asia/Tokyo）。"
                        "已报名者还需要提交成绩单，否则申请材料不完整。</p></body></html>").encode()
                status = "200 OK"
            else:
                body, status = b"unknown fixture document", "404 Not Found"
            writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                          f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
            await writer.drain()
            if path == b"/project":
                state["served"] += 1
        except (OSError, TimeoutError, ValueError, IndexError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            try:
                writer.close()
                with suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 2)
            finally:
                handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state["url"] = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/project"
    try:
        yield state
    finally:
        gate.set()
        server.close()
        await server.wait_closed()
        pending = tuple(handlers)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        state["cleanup_completed"] = not handlers


def background_facts(value: str, nonce: str) -> dict[str, bool]:
    """Controlled fixture coverage checks, without exporting answer text."""
    return {
        "source_nonce": nonce in value,
        "deadline_date": bool(re.search(r"2026\s*(?:年|[-/.])\s*11\s*(?:月|[-/.])\s*18(?:\s*日|\b)", value)),
        "deadline_time": bool(re.search(r"(?<!\d)18\s*(?:(?:[:：]\s*00)(?!\d)|(?:点|時|时)(?:整|00分)?(?!\s*(?:\d|半|一刻|三刻)))", value)),
        "deadline_timezone": bool(re.search(r"Asia/Tokyo|JST|(?:UTC|GMT)\s*\+\s*0?9\b|\+09:00|东京|東京|日本(?:时间|時間)", value, re.IGNORECASE)),
        "required_material": "成绩单" in value or "成績証明" in value or "transcript" in value.lower(),
    }


async def background(args) -> dict:
    """Natural-language delegation, concurrent chat, callback and later retrieval."""
    from agent.plugin_composition.config_input import save_config
    from core.common.file_io import run_file_io
    from eval.models import EvaluationModel, source_identity
    from eval.native_runtime import text
    from kirakira_agent.runtime import ROOT, initialize_workspace, rpc, runtime
    from plugins.delivery.contract import DELIVERY_READ
    from plugins.ledger.contract import (
        BINDINGS,
        MESSAGE_CATALOG,
        CallRef,
        Control,
        Input,
        Output,
        ToolCall,
        ToolResult,
        json_value,
    )
    from plugins.subagent.request import Request
    from plugins.tools.contract import TOOLS

    plan = ("background-admission", "ordinary-chat-during-research", "actual-source-result",
            "completion-notification", "query-completed-result", "result-after-restart")
    nonce = uuid4().hex[:16]
    session = "akashic:background-" + uuid4().hex
    report = {"scope": "Controlled real-model ordinary-language URL delegation through native tools. Gated synthetic HTTP document, no tool names in user prompts. Local delivery means a committed message, not a read receipt or Telegram measurement.",
              "cases": [{"case": label, "status": "not_run", "passed": False} for label in plan],
              "planned_cases": len(plan), "passed": False, "setup_passed": False, "shutdown_completed": False,
              "fixture_configuration": {"standard_web.default_fetch_timeout_seconds": 120,
                  "document_gate_budget_seconds": 120, "parent_admission_timeout_seconds": 60,
                  "quality_scope": "single controlled task, fact coverage; not a public benchmark"}}
    active_case, source = None, None
    saved_environment = {key: os.environ.get(key) for key in ("AKASHIC_PLUGIN_HOME", "NO_PROXY", "no_proxy")}

    def save():
        write_report(args.output, report)

    def start_case(label):
        nonlocal active_case
        active_case = label
        row = next(row for row in report["cases"] if row["case"] == label)
        row.update(status="running", passed=False)
        save()
        return row

    def finish_case(row, passed, **facts):
        row.update(facts)
        row.update(passed=bool(passed), status="passed" if passed else "failed")
        save()

    async def wait_read(read, *, timeout=90):
        async with asyncio.timeout(timeout):
            while True:
                value = await read()
                if value is not None:
                    return value
                await asyncio.sleep(.05)

    def named_calls(messages, bindings):
        return [(bindings.describe(part.binding_id, TOOLS)["tool"]["name"], part,
                 CallRef(message.message_id, index))
                for message in messages if isinstance(message.body, Output)
                for index, part in enumerate(message.body.parts) if isinstance(part, ToolCall)]

    def successful_results(messages):
        return {message.body.call_ref: message for message in messages
                if isinstance(message.body, ToolResult) and message.body.outcome == "success"}

    def terminal_message(messages):
        return next((message for message in reversed(messages) if message.source == "subagent" and (
            isinstance(message.body, Output) and message.body.finish in {"complete", "quiet"}
            or isinstance(message.body, Control) and message.body.action in {"failure", "pause", "abandon"})), None)

    def original_result_proof(messages, calls, job_id, terminal, notice_id):
        results = successful_results(messages)
        original = text(terminal)
        for name, call, ref in calls:
            if (name != "spawn_manage" or call.arguments.get("action") != "result"
                or call.arguments.get("job_id") != job_id or ref not in results):
                continue
            try:
                value = json.loads(text(results[ref]))
                window = value["result"]
                provenance = window["source"]
                offset, end = window["offset"], window["end_offset"]
                notification = value["notification"]
                if (value["job_id"] == job_id and value["work_status"] == "completed"
                    and provenance["session_id"] == terminal.session_id
                    and provenance["message_id"] == terminal.message_id and provenance["seq"] == terminal.seq
                    and provenance["recorded_at"] == terminal.recorded_at.isoformat()
                    and provenance["body_kind"] == "output"
                    and type(offset) is int and type(end) is int and 0 <= offset <= end <= len(original)
                    and window["source_chars"] == len(original) and window["text"] == original[offset:end]
                    and nonce in window["text"] and notification["status"] == "delivered"
                    and notification["message"]["message_id"] == notice_id):
                    return True
            except (ValueError, KeyError, TypeError):
                continue
        return False

    async def exercise_turn(core, client, label, prompt, *, timeout=150):
        observed = start_case(label)
        context = core.plugin_manager.live_root.context
        catalog, bindings = context.require(MESSAGE_CATALOG), context.require(BINDINGS)
        row = await asyncio.wait_for(measure_turn(client, session, prompt, expected_source="conversation"), timeout)
        reader = catalog.reader(session)
        answer = await run_file_io(lambda: reader.get(row.pop("ending_message_id", "")))
        content = text(answer) if answer is not None else ""
        messages = await reader.read_async(lambda current: current.snapshot(
            after_seq=row["after_seq"], through_seq=row["through_seq"]))
        actual_input = next((message for message in messages if message.message_id == row["input_message_id"]), None)
        canonical = (actual_input is not None and isinstance(actual_input.body, Input)
            and actual_input.source == "conversation" and actual_input.author == "user"
            and answer is not None and isinstance(answer.body, Output) and answer.body.finish == "complete"
            and answer.source == "conversation" and answer.seq > actual_input.seq
            and not any(isinstance(message.body, Input) and message.source == "conversation"
                        and actual_input.seq < message.seq < answer.seq for message in messages))
        calls = named_calls([message for message in messages if message.source == "conversation"], bindings)
        row.update(tools=[name for name, _, _ in calls], canonical_input_output_bound=canonical)
        row["passed"] = row["passed"] and canonical
        row["model_calls"] = await run_file_io(lambda: turn_model_metrics(args.workspace, session, row))
        observed.update(row)
        observed["passed"] = False  # A terminal alone does not pass the case's factual checks.
        save()
        return observed, row["passed"], content, messages, calls

    save()
    try:
        if args.workspace.exists():
            raise ValueError("background exercises require a new isolated workspace")
        os.environ["AKASHIC_PLUGIN_HOME"] = str(args.workspace / "isolated-plugin-home")
        bypass = ",".join(filter(None, [os.environ.get("NO_PROXY"), os.environ.get("no_proxy"), "127.0.0.1,localhost,::1"]))
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = bypass
        initialize_workspace(args.workspace)
        # This is an isolated experiment input, not a production default change.
        save_config(args.workspace / "plugin-data/standard_web-builtin", {"default_fetch_timeout": 120})
        extension = args.workspace / "fixtures"
        shutil.copytree(ROOT / "eval/fixtures/alignment", extension / "alignment", ignore=shutil.ignore_patterns("__pycache__"))
        model = EvaluationModel.read(args.model_config)
        report.update(implementation=source_identity(), model_config=model.description())
        async with background_source(nonce) as source:
            async with runtime(args.workspace, extensions=(extension,)) as core:
                await model.configure(core, args.key_file)
                report["setup_passed"] = True
                await rpc(core, "evaluation/seed", {"session_id": session, "visibility": "internal"})
                context = core.plugin_manager.live_root.context
                catalog, history = context.require(MESSAGE_CATALOG), context.require(DELIVERY_READ)
                async with await AsyncClient.for_workspace(args.workspace) as client:
                    try:
                        first, terminal_ok, answer, messages, calls = await exercise_turn(core, client, "background-admission",
                            f"这份资料 {source['url']} 帮我在后台查清楚：申请截止时间、还要补什么材料、核验码。"
                            "资料源允许最多等待两分钟。先确认收到，让我能继续聊别的；等调查真正完成，再主动把查到的结果告诉我。", timeout=60)
                        results, admitted_jobs = successful_results(messages), []
                        for name, call, ref in calls:
                            if name != "spawn" or call.arguments.get("run_in_background") is not True or ref not in results:
                                continue
                            for job_id in re.findall(r"job_id=([a-f0-9]{32})", text(results[ref])):
                                admitted = await run_file_io(lambda identity=job_id: catalog.reader("subagent:" + identity).get("subagent-input:" + identity))
                                if admitted is None or not isinstance(admitted.body, Input):
                                    continue
                                records = [part for part in admitted.body.parts if part.kind == "subagent.request"]
                                if len(records) != 1:
                                    continue
                                request = Request.model_validate(json_value(records[0].value))
                                if (request.job_id == job_id and request.background and request.parent_session_id == session
                                    and request.parent_message_id == ref.message_id and request.parent_part_index == ref.part_index):
                                    admitted_jobs.append(job_id)
                        finish_case(first, terminal_ok and len(admitted_jobs) == 1 and source["served"] == 0 and bool(answer.strip()),
                            background_tool_admitted=len(admitted_jobs) == 1, successful_spawn_results=len(admitted_jobs),
                            acknowledged_before_document_release=source["served"] == 0, public_acknowledgement_present=bool(answer.strip()))
                        if not first["passed"]:
                            raise RuntimeError("background admission prerequisite failed")
                        job_id = admitted_jobs[0]
                        child = catalog.reader("subagent:" + job_id)
                        start_case("ordinary-chat-during-research")
                        await asyncio.wait_for(source["requested"].wait(), 60)
                        remaining = max(.01, 120 - (time.monotonic() - source["first_requested_at"]))
                        chat, terminal_ok, content, _, _ = await exercise_turn(core, client, "ordinary-chat-during-research",
                            "先问个别的：37加48等于多少？只回答数字。", timeout=remaining)
                        child_rows = await child.read_async(lambda reader: reader.snapshot())
                        child_calls = named_calls(child_rows, context.require(BINDINGS))
                        settled = {message.body.call_ref for message in child_rows if isinstance(message.body, ToolResult)}
                        waiting_fetch = any(name == "web_fetch" and call.arguments.get("url") == source["url"] and ref not in settled
                                            for name, call, ref in child_calls)
                        ongoing = terminal_message(child_rows) is None and waiting_fetch
                        finish_case(chat, terminal_ok and content.strip() == "85" and ongoing and source["served"] == 0,
                            answer_correct=content.strip() == "85", research_still_waiting=ongoing,
                            pending_source_tool_call=waiting_fetch, document_not_released=source["served"] == 0)
                    finally:
                        source["gate"].set()

                    actual = start_case("actual-source-result")
                    async def completed():
                        return terminal_message(await child.read_async(lambda reader: reader.snapshot()))

                    terminal = await wait_read(completed)
                    raw = text(terminal) if isinstance(terminal.body, Output) else ""
                    child_rows = await child.read_async(lambda reader: reader.snapshot())
                    child_results = successful_results(child_rows)
                    read_nonce = any(name == "web_fetch" and call.arguments.get("url") == source["url"]
                        and ref in child_results and nonce in text(child_results[ref])
                        for name, call, ref in named_calls(child_rows, context.require(BINDINGS)))
                    facts = background_facts(raw, nonce)
                    finish_case(actual, isinstance(terminal.body, Output) and read_nonce and all(facts.values()),
                        source_read_by_tool=read_nonce, fact_coverage=facts, result_chars=len(raw),
                        work_completed=isinstance(terminal.body, Output))
                    if not actual["passed"]:
                        raise RuntimeError("source result prerequisite failed")

                    notification_case = start_case("completion-notification")
                    async def notified():
                        rows = await catalog.reader(session).read_async(lambda reader: reader.snapshot())
                        notices = [row for row in rows if row.source == child.session_id and isinstance(row.body, Output)
                                   and row.body.finish in {"complete", "quiet"}]
                        if notices:
                            receipt = await run_file_io(lambda: history.status(notices[-1].message_id, "akashic"))
                            confirmed = (receipt or {}).get("receipt") or {}
                            if confirmed.get("status") in {"delivered", "failed", "rejected"}:
                                return notices, confirmed
                        return None

                    notices, receipt = await wait_read(notified)
                    notice_facts = background_facts(text(notices[0]), nonce)
                    finish_case(notification_case, len(notices) == 1 and receipt["status"] == "delivered" and all(notice_facts.values()),
                        final_notices=len(notices), delivery_state=receipt["status"],
                        source_nonce_in_notice=notice_facts["source_nonce"], fact_coverage=notice_facts)
                    if not notification_case["passed"]:
                        raise RuntimeError("completion notification prerequisite failed")
                    notice_id = notices[0].message_id
                    queried, terminal_ok, answer, messages, calls = await exercise_turn(core, client, "query-completed-result",
                        "刚才交给你的后台任务现在完成了吗？请核对任务记录，读出它保存的原始结果，并说明完成通知是否已经送达。")
                    proven = original_result_proof(messages, calls, job_id, terminal, notice_id)
                    finish_case(queried, terminal_ok and proven and nonce in answer,
                        queried_durable_status=proven, read_original_result=proven, source_nonce_in_answer=nonce in answer)
                requests_before_restart = source["requests"]

            async with (runtime(args.workspace, extensions=(extension,)) as core,
                        await AsyncClient.for_workspace(args.workspace) as client):
                restarted, terminal_ok, answer, messages, calls = await exercise_turn(core, client, "result-after-restart",
                    "请重新核对刚才后台任务保存的原始结果，我要它的核验码和来源记录；不要只凭聊天中的概括回答。")
                proven = original_result_proof(messages, calls, job_id, terminal, notice_id)
                current = core.plugin_manager.live_root.context.require(MESSAGE_CATALOG).reader(child.session_id)
                unchanged = await run_file_io(lambda: current.get(terminal.message_id)) == terminal
                finish_case(restarted, terminal_ok and proven and nonce in answer and unchanged and source["requests"] == requests_before_restart,
                    source_nonce_in_answer=nonce in answer, read_original_result=proven,
                    saved_result_unchanged=unchanged, source_not_refetched=source["requests"] == requests_before_restart)
            report["restart_completed"] = True
        report["shutdown_completed"] = True
    except Exception as error:  # noqa: BLE001 - export safe failure facts, never provider bodies
        report["failure"] = {"case": active_case or "setup", "type": type(error).__name__,
                             "http_statuses": sorted({int(value) for value in re.findall(r"\bHTTP\s*([1-5]\d{2})\b", str(error))})}
        frames, trace = [], error.__traceback__
        while trace is not None:
            try:
                path = Path(trace.tb_frame.f_code.co_filename).resolve().relative_to(ROOT)
            except ValueError:
                pass
            else:
                frames.append({"file": str(path), "line": trace.tb_lineno,
                               "function": trace.tb_frame.f_code.co_name})
            trace = trace.tb_next
        report["failure"]["source_frames"] = frames
        if active_case is not None:
            row = next(row for row in report["cases"] if row["case"] == active_case)
            if row["status"] == "running":
                finish_case(row, False, failure=report["failure"])
    finally:
        if source is not None:
            report.update(source_requests=source["requests"], source_responses=source["served"],
                source_gate_wait_ms=source["longest_wait_ms"], fixture_gate_timeouts=source["fixture_timeouts"],
                source_cleanup_completed=source["cleanup_completed"])
        for row in report["cases"]:
            if row["status"] == "not_run":
                row["blocked_by"] = (report.get("failure") or {}).get("case", "setup")
        report["case_counts"] = {"planned": len(plan), **{
            status: sum(row["status"] == status for row in report["cases"])
            for status in ("passed", "failed", "not_run")}}
        report["passed"] = (report["setup_passed"] and report["shutdown_completed"]
                            and not report.get("failure") and all(row["passed"] for row in report["cases"]))
        for key, value in saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        save()
    return report


def storage(args) -> dict:
    from plugins.content.plugin import check_text
    from plugins.ledger.contract import ContentPart, Input
    from plugins.ledger.log import MessageLog

    report = {"scope": "Sequential small disposable I/O; cached reads, not a cold-cache or throughput benchmark. No production database writes.", "filesystems": {}}
    for label, parent in (("workspace_storage", args.nfs_directory), ("comparison_storage", args.local_directory)):
        parent.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="kirakira-probe-", dir=parent))
        values = {"sqlite_commit_ms": [], "file_fsync_ms": [], "cached_read_ms": [],
                  "native_ledger_append_ms": [], "native_ledger_read_ms": []}
        try:
            database = folder / "probe.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("CREATE TABLE probe(k INTEGER PRIMARY KEY,v TEXT)")
            for number in range(args.samples):
                begin = time.perf_counter()
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("INSERT INTO probe VALUES(?,?)", (number, "x" * 1024))
                values["sqlite_commit_ms"].append((time.perf_counter() - begin) * 1000)
                path = folder / "value.txt"
                begin = time.perf_counter()
                with path.open("wb") as stream:
                    stream.write(b"x" * 4096)
                    stream.flush()
                    os.fsync(stream.fileno())
                values["file_fsync_ms"].append((time.perf_counter() - begin) * 1000)
                begin = time.perf_counter()
                assert len(path.read_bytes()) == 4096
                values["cached_read_ms"].append((time.perf_counter() - begin) * 1000)
            # Use the production ledger's actual WAL/NORMAL connection policy
            # as well: the FULL/DELETE microbenchmark above is not its latency.
            log = MessageLog(folder / "sessions.db")
            try:
                writer = log.writer("probe", author="evaluation", source="evaluation",
                                    body_types=(Input,), content={"text": check_text})
                for number in range(args.samples):
                    identity = "probe-" + str(number)
                    begin = time.perf_counter()
                    message = writer.append(identity, Input((ContentPart("text", "x" * 1024),)))
                    values["native_ledger_append_ms"].append((time.perf_counter() - begin) * 1000)
                    begin = time.perf_counter()
                    assert log.reader("probe").get(identity) == message
                    values["native_ledger_read_ms"].append((time.perf_counter() - begin) * 1000)
            finally:
                log.close()
            report["filesystems"][label] = {key: {"samples_ms": samples, "median_ms": statistics.median(samples),
                                                    "max_ms": max(samples)} for key, samples in values.items()}
        finally:
            shutil.rmtree(folder)
    return report


@asynccontextmanager
async def loopback_model():
    """A deterministic HTTP peer; never replace the native Models service."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # No request headers, payloads, credential URLs or bodies.

        def send(self, value, *, status=200, content_type="application/json"):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def do_GET(self):
            if urlsplit(self.path).path != "/v1/models":
                self.send({"error": "unknown fixture route"}, status=404)
                return
            self.send({"object": "list", "data": [{"id": "local-latency-fixture", "object": "model"}]})

        def do_POST(self):
            if urlsplit(self.path).path != "/v1/chat/completions":
                self.send({"error": "unknown fixture route"}, status=404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8 * 1024 * 1024:
                    self.close_connection = True
                    self.send({"error": "fixture request size outside limit"}, status=413)
                    return
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict) or request.get("model") != "local-latency-fixture":
                    self.send({"error": "invalid fixture model"}, status=400)
                    return
            except (ValueError, OSError):
                self.close_connection = True
                self.send({"error": "invalid fixture request"}, status=400)
                return
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            if request.get("stream"):
                chunks = [
                    {"id": "fixture", "choices": [{"index": 0, "delta": {
                        "role": "assistant", "content": "FIXTURE_ACK"}, "finish_reason": None}]},
                    {"id": "fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
                ]
                body = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                self.send(body, content_type="text/event-stream")
            else:
                self.send({"id": "fixture", "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "FIXTURE_ACK"}, "finish_reason": "stop"}], "usage": usage})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port) + "/v1"
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=2)


async def mechanism(args) -> dict:
    """Compare real native scheduling and I/O with remote generation removed."""
    from eval.telegram_latency import evaluate

    if not args.embedding_manifest.is_file():
        raise ValueError("mechanism requires an existing local embedding manifest")
    if args.memory_documents is not None and not args.memory_documents.is_dir():
        raise ValueError("memory-documents must be an existing directory")
    report = {"scope": "Deterministic loopback HTTP model through real native Runtime, Models, memory and tools. Mechanism only; not model or memory quality evidence.",
              "model_is_fixture": True, "usage_is_synthetic": True, "chat_endpoint_scope": "loopback only",
              "cases_per_filesystem": ["natural-greeting", "natural-followup"], "runs": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    saved_environment = {name: os.environ.get(name) for name in ("AKASHIC_PLUGIN_HOME", "AKASHIC_TIMING", "NO_PROXY", "no_proxy")}
    bypass = ",".join(filter(None, [os.environ.get("NO_PROXY"), os.environ.get("no_proxy"), "127.0.0.1,localhost,::1"]))
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = bypass
    try:
        with tempfile.TemporaryDirectory(prefix="kira-mechanism-", dir=args.output.parent) as config_directory:
            configuration = Path(config_directory)
            key_file = configuration / "fixture.key"
            key_file.write_text("evaluation-fixture-not-a-credential")
            async with loopback_model() as endpoint:
                model_config = configuration / "fixture.json"
                write_report(model_config, {"driver_id": "openai-compatible", "model": "local-latency-fixture",
                    "endpoint": endpoint, "model_id": "evaluation", "context_window": 32768, "max_output_tokens": 4096})
                for label, parent in (("nfs", args.nfs_directory), ("local", args.local_directory)):
                    parent = parent.resolve()
                    parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="kira-io-", dir=parent) as directory:
                        workspace = Path(directory) / "w"
                        if len(os.fsencode(workspace / "runtime/web-chat.sock")) >= 108:
                            raise ValueError("choose shorter mechanism parent directories for Linux Unix sockets")
                        row = {"label": label, "passed": False}
                        try:
                            row["filesystem_type"] = subprocess.check_output(
                                ["stat", "-f", "-c", "%T", str(parent)], text=True, stderr=subprocess.DEVNULL).strip()
                        except (OSError, subprocess.CalledProcessError):
                            row["filesystem_type"] = "unknown"
                        trial_output = configuration / (label + ".json")
                        trial = SimpleNamespace(workspace=workspace, key_file=key_file, model_config=model_config,
                            embedding_manifest=args.embedding_manifest, output=trial_output,
                            memory_documents=args.memory_documents, telegram_token_file=None, telegram_chat_file=None,
                            expected_bot=None, case_names=["natural-greeting", "natural-followup"],
                            no_direct_replay=True, file_io_trace=True)
                        try:
                            row["passed"] = await evaluate(trial)
                        except BaseException as error:
                            row["error_type"] = type(error).__name__
                            raise
                        finally:
                            if trial_output.exists():
                                row["native"] = json.loads(trial_output.read_text())
                            report["runs"].append(row)
                            write_report(args.output, report)
        report["passed"] = len(report["runs"]) == 2 and all(row["passed"] for row in report["runs"])
        report["temporary_workspaces_removed"] = True
        return report
    finally:
        for name, value in saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@asynccontextmanager
async def browser_fixture(args):
    from fastapi import FastAPI, Request, WebSocket
    from fastapi.responses import Response, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    from websockets.asyncio.client import unix_connect
    from eval.models import EvaluationModel
    from kirakira_agent.runtime import ROOT, initialize_workspace, rpc, runtime

    if args.workspace.exists():
        raise ValueError("browser exercises require a new isolated workspace")
    if len(os.fsencode(args.workspace.resolve() / "runtime/web-chat.sock")) >= 108:
        raise ValueError("choose a shorter browser workspace path for Linux Unix sockets")
    for assets in (args.assets, args.comparison_assets):
        if assets is not None and not (assets / "index.html").is_file():
            raise ValueError("browser exercises require compiled standalone assets")
    os.environ["AKASHIC_PLUGIN_HOME"] = str(args.workspace.parent / (args.workspace.name + "-plugins"))
    initialize_workspace(args.workspace, profile="web")
    extension = args.workspace / "fixtures"
    shutil.copytree(ROOT / "eval/fixtures/alignment", extension / "alignment", ignore=shutil.ignore_patterns("__pycache__"))
    async with runtime(args.workspace, extensions=(extension,)) as core:
        if args.model_config:
            await EvaluationModel.read(args.model_config).configure(core, args.key_file)
        for number in range(3):
            await rpc(core, "evaluation/seed", {"session_id": "akashic:experience-" + str(number), "records": [
                {"kind": "input", "text": "请记住项目 KIRA-LIVE 的预算为760元，截止日期为11月8日。"},
                {"kind": "output", "text": f"会话 {number}：项目记录已确认，预算760元，截止11月8日。"}]})
        app = FastAPI()
        transport = httpx.AsyncHTTPTransport(uds=str(args.workspace / "runtime/web-chat.sock"))
        async with httpx.AsyncClient(transport=transport, base_url="http://local", timeout=30) as http:
            @app.get("/")
            async def root():
                return RedirectResponse("/assets/")

            @app.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"])
            async def api(path: str, request: Request):
                url = "/api/" + path + ("?" + request.url.query if request.url.query else "")
                response = await http.request(request.method, url, content=await request.body(),
                    headers={k: v for k, v in request.headers.items() if k.lower() in {"content-type", "accept", "x-requested-with"}})
                return Response(response.content, response.status_code, headers={k: v for k, v in response.headers.items()
                    if k.lower() not in {"transfer-encoding", "content-encoding", "content-length", "connection"}})

            @app.websocket("/ws")
            async def ws(socket: WebSocket):
                await socket.accept()
                path = "/ws" + ("?" + socket.url.query if socket.url.query else "")
                origin = socket.headers.get("origin", "http://127.0.0.1")
                async with unix_connect(str(args.workspace / "runtime/web-chat.sock"),
                    uri=origin.replace("http", "ws", 1) + path, origin=origin, proxy=None) as remote:
                    async def incoming():
                        while True:
                            await remote.send(await socket.receive_text())

                    async def outgoing():
                        async for frame in remote:
                            await socket.send_text(frame)
                    tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

            static = StaticFiles(directory=str(args.assets), html=True)
            app.mount("/assets", static)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
            running = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(20):
                    while not server.started:
                        if running.done():
                            await running
                            raise RuntimeError("browser fixture stopped before becoming ready")
                        await asyncio.sleep(0.02)
                yield "http://127.0.0.1:" + str(listener.getsockname()[1]), static
            finally:
                server.should_exit = True
                await running
                listener.close()


async def browser(args) -> dict:
    from playwright.async_api import async_playwright

    report = {"scope": "Real compiled chat UI, Chromium and isolated native backend with synthetic conversations. latency_ms is the Chromium network-emulation parameter, not measured packet RTT.",
              "latency_ms": args.latency_ms, "runs": []}
    async with browser_fixture(args) as (url, static), async_playwright() as playwright:
        executable = {"executable_path": args.browser_executable} if args.browser_executable else {}
        chromium = await playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"], **executable)
        try:
            choices = [("current", args.assets)] if args.comparison_assets is None else [("comparison", args.comparison_assets), ("current", args.assets)]
            for label, assets in choices:
                static.directory = str(assets)
                static.all_directories = [str(assets)]
                async with await chromium.new_context(viewport={"width": 1440, "height": 1000}) as context:
                    page = await context.new_page()
                    sockets, errors = [], []
                    page.on("websocket", lambda socket: sockets.append(socket.url))
                    page.on("pageerror", lambda error: errors.append(type(error).__name__))
                    cdp = await context.new_cdp_session(page)
                    await cdp.send("Network.enable")
                    await cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": args.latency_ms,
                        "downloadThroughput": 12500000, "uploadThroughput": 12500000})
                    begin = time.monotonic()
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.locator(".conversation-session").first.wait_for(timeout=45000)
                    row = {"label": label, "sidebar_ready_ms": (time.monotonic() - begin) * 1000, "switches": []}
                    for index in (0, 1, 2, 0, 1, 2):
                        await page.wait_for_timeout(250)
                        begin, socket_start = time.monotonic(), len(sockets)
                        await page.locator(".conversation-session").nth(index).click()
                        await page.get_by_text(f"会话 {2-index}：项目记录已确认，预算760元，截止11月8日。", exact=False).last.wait_for(timeout=30000)
                        visible = (time.monotonic() - begin) * 1000
                        await page.wait_for_timeout(250)
                        row["switches"].append({"visible_ms": visible, "new_websockets": len(sockets) - socket_start})
                    if label == "current":
                        source = page.locator(".conversation-session").nth(0)
                        form = page.locator("form.composer")
                        chips = page.locator(".composer-reference")
                        await source.drag_to(form)
                        await chips.first.wait_for()
                        await source.drag_to(form)
                        await page.get_by_text("这个会话已经引用过了", exact=True).wait_for()
                        duplicate_rejected = await chips.count() == 1
                        await page.locator(".conversation-session").nth(1).click()
                        await page.wait_for_timeout(250)
                        isolated = await chips.count() == 0
                        await page.locator(".conversation-session").nth(2).click()
                        await chips.first.wait_for()
                        restored = await chips.count() == 1
                        await chips.get_by_role("button").click()
                        removed = await chips.count() == 0
                        await page.locator(".conversation-session").nth(2).drag_to(form)
                        await page.get_by_text("不能把当前会话引用给自己", exact=True).wait_for()
                        row["reference_composer"] = {"duplicate_rejected": duplicate_rejected,
                            "drafts_isolated": isolated, "draft_restored": restored,
                            "reference_removed": removed, "self_reference_rejected": await chips.count() == 0}
                    row.update(total_websockets=len(sockets), browser_errors=errors, passed=not errors)
                    if "reference_composer" in row:
                        row["passed"] = row["passed"] and all(row["reference_composer"].values())
                    report["runs"].append(row)
                    write_report(args.output, report)
        finally:
            await chromium.close()
    return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("service")
    live.add_argument("--workspace", type=Path, required=True)
    live.add_argument("--exercise", action="store_true", help="Send synthetic internal SDK and CLI turns; excluded from memory learning")
    live.add_argument("--samples", type=int, default=5)
    disk = commands.add_parser("storage")
    disk.add_argument("--nfs-directory", type=Path, required=True)
    disk.add_argument("--local-directory", type=Path, required=True)
    disk.add_argument("--samples", type=int, default=24)
    diagnostic = commands.add_parser("mechanism", help="Deterministic loopback model and real native I/O; not quality evidence")
    diagnostic.add_argument("--nfs-directory", type=Path, required=True)
    diagnostic.add_argument("--local-directory", type=Path, required=True)
    diagnostic.add_argument("--embedding-manifest", type=Path, required=True)
    diagnostic.add_argument("--memory-documents", type=Path)
    web = commands.add_parser("browser")
    web.add_argument("--workspace", type=Path, required=True)
    web.add_argument("--assets", type=Path, required=True, help="Compiled standalone chat harness directory")
    web.add_argument("--comparison-assets", type=Path)
    web.add_argument("--model-config", type=Path)
    web.add_argument("--key-file", type=Path)
    web.add_argument("--browser-executable")
    web.add_argument("--latency-ms", type=int, default=80)
    linked = commands.add_parser("references")
    linked.add_argument("--workspace", type=Path, required=True)
    linked.add_argument("--model-config", type=Path, required=True)
    linked.add_argument("--key-file", type=Path, required=True)
    delegated = commands.add_parser("background", help="Natural-language background research, normal chat, callback and restart retrieval")
    delegated.add_argument("--workspace", type=Path, required=True)
    delegated.add_argument("--model-config", type=Path, required=True)
    delegated.add_argument("--key-file", type=Path, required=True)
    for command in (live, disk, diagnostic, web, linked, delegated):
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if hasattr(args, "samples") and not 1 <= args.samples <= 100:
        parser.error("samples must be between 1 and 100")
    if args.command == "browser" and bool(args.model_config) != bool(args.key_file):
        parser.error("model-config and key-file must be supplied together")
    if args.command == "browser" and args.latency_ms < 0:
        parser.error("latency-ms cannot be negative")
    operations = {"service": service, "browser": browser, "references": references, "background": background, "mechanism": mechanism}
    report = storage(args) if args.command == "storage" else asyncio.run(operations[args.command](args))
    write_report(args.output, report)
    print(json.dumps({"report": str(args.output), "command": args.command}))
    if any(not row["passed"] for row in report.get("cases", []) + report.get("runs", [])):
        raise SystemExit(1)
    if any(row["status"] != 200 for row in report.get("read_only", [])):
        raise SystemExit(1)
    if report.get("passed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
