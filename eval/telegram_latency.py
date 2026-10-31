"""Measure native latency, optional real Telegram transport, and identical wire replay.

Telegram mode sends controlled replies to an explicitly supplied private chat.
It disables getUpdates in the isolated runtime; the production bot keeps polling.
Synthetic inbound updates are identified as such, never called phone-to-phone tests.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import threading
from collections import Counter, deque
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from core.common.file_io import run_file_io
from eval.models import EvaluationModel, provider_slot, source_identity
from eval.native_runtime import text
from kirakira_agent.client import AsyncClient
from kirakira_agent.runtime import ROOT, initialize_workspace, rpc, runtime
from kirakira_agent.setup import configure_embedding
from plugins.channels.contract import CHANNELS
from plugins.compaction.contract import COMPACTION_SUMMARIES
from plugins.delivery.contract import DELIVERY_READ
from plugins.ledger.contract import MESSAGE_CATALOG, Control, Output, ToolResult


class Timings(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        fields = getattr(record, "akashic_fields", {})
        if fields.get("event") == "runtime.timing":
            self.rows.append(dict(fields))


class Capture:
    """Observe actual generation-local drivers; wire bytes remain only in RAM."""

    def __init__(self):
        self.calls = []
        self.active = ContextVar("latency_capture", default=None)

    def install(self, stack):
        seen = set()
        for module in tuple(sys.modules.values()):
            cls = getattr(module, "_BoundChat", None)
            if not isinstance(cls, type) or not hasattr(cls, "_encode_body") or cls in seen:
                continue
            seen.add(cls)
            self.install_class(stack, cls)
            module = sys.modules[cls.__module__]
            if hasattr(module, "_consume_stream"):
                self.install_stream(stack, module)

    def install_stream(self, stack, module):
        original = module._consume_stream

        async def consume(response, callback, **options):
            row = self.active.get()
            original_lines = response.aiter_lines
            tail = ""

            async def lines():
                nonlocal tail
                async for line in original_lines():
                    if row is not None and line.startswith("data:") and line[5:].strip() != "[DONE]":
                        try:
                            chunk = json.loads(line[5:])
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                content = delta.get("content") or ""
                                if content:
                                    row.setdefault("first_raw_content_at", time.monotonic())
                                    joined = tail + content
                                    if "<think>" in joined:
                                        row["legacy_think_tag_seen"] = True
                                    if "</think>" in joined:
                                        row.setdefault("legacy_close_at", time.monotonic())
                                    tail = joined[-16:]
                        except (ValueError, AttributeError, TypeError):
                            pass  # Observation never changes driver validation.
                    yield line

            with patch.object(response, "aiter_lines", lines):
                return await original(response, callback, **options)

        stack.enter_context(patch.object(module, "_consume_stream", consume))

    def install_class(self, stack, cls):
        original_encode, original_complete = cls._encode_body, cls.complete

        def encode(bound, body, request):
            data = original_encode(bound, body, request)
            if (row := self.active.get()) is not None:
                row["body"] = data
                row["purpose"] = "summary" if "[Source messages]" in str(request.messages) else "reply"
            return data

        async def complete(bound, request):
            row = {"begin": time.monotonic(), "first_content_at": None}
            token = self.active.set(row)
            callback = request.on_delta

            async def delta(value):
                if row["first_content_at"] is None and value.get("content_delta"):
                    row["first_content_at"] = time.monotonic()
                await callback(value)

            try:
                response = await original_complete(bound, replace(request, on_delta=delta) if callback else request)
                row["usage"] = None if response.usage is None else asdict(response.usage)
                row["thinking_chars"] = len(response.thinking or "")
                return response
            except Exception as error:
                row.update(failed=True, error_type=type(error).__name__)
                if status := re.search(r"HTTP (\d{3})", str(error)):
                    row["http_status"] = int(status[1])
                raise
            finally:
                row["duration_ms"] = (time.monotonic() - row["begin"]) * 1000
                self.calls.append(row)
                self.active.reset(token)

        stack.enter_context(patch.object(cls, "_encode_body", encode))
        stack.enter_context(patch.object(cls, "complete", complete))


class FileIoCapture:
    """Separate bounded-worker admission from physical work, without arguments."""

    def __init__(self):
        self.rows = []
        self.samples = deque(maxlen=20000)
        self.worker_samples = deque(maxlen=20000)
        self.workers = {}

    def install(self, stack):
        from core.common.file_io import run_file_io

        loop_thread = threading.get_ident()
        stopped = threading.Event()

        def sample():
            while not stopped.wait(0.02):
                current = sys._current_frames()
                targets = [(loop_thread, None), *tuple(self.workers.items())]
                for identity, operation in targets:
                    frame = current.get(identity)
                    frames = []
                    try:
                        while frame is not None and len(frames) < 9:
                            frames.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}")
                            frame = frame.f_back
                        if operation is None:
                            self.samples.append((time.monotonic(), tuple(frames)))
                        else:
                            self.worker_samples.append((id(operation), tuple(frames)))
                    finally:
                        del frame
                del current

        sampler = threading.Thread(target=sample, name="latency-stack-sampler", daemon=True)
        sampler.start()

        def stop():
            stopped.set()
            sampler.join(timeout=2)

        stack.callback(stop)

        async def observed(function):
            row = {"begin": time.monotonic(), "operation": getattr(function, "__qualname__", type(function).__name__)}

            def work():
                row["worker_begin"] = time.monotonic()
                identity = threading.get_ident()
                self.workers[identity] = row
                try:
                    return function()
                finally:
                    row["worker_end"] = time.monotonic()
                    self.workers.pop(identity, None)

            try:
                return await run_file_io(work)
            finally:
                row["duration_ms"] = (time.monotonic() - row["begin"]) * 1000
                if "worker_begin" in row:
                    row["queue_ms"] = (row["worker_begin"] - row["begin"]) * 1000
                    row["work_ms"] = (row["worker_end"] - row["worker_begin"]) * 1000
                self.rows.append(row)

        for module in tuple(sys.modules.values()):
            if getattr(module, "run_file_io", None) is run_file_io:
                stack.enter_context(patch.object(module, "run_file_io", observed))

    def summary(self, start):
        rows = [item for item in self.rows if item["begin"] >= start]
        worker_stacks = {}
        for identity, frames in tuple(self.worker_samples):
            worker_stacks.setdefault(identity, Counter())[frames] += 1
        return {"scope": "All observed file-worker operations started during this turn, including concurrent background work. Queue includes executor scheduling; work may include locks, CPU and filesystem wait.",
                "operations": len(rows), "slowest": [
                    {key: value for key, value in item.items() if key in {"operation", "duration_ms", "queue_ms", "work_ms"}}
                    | {"worker_stacks": [{"frames": frames, "samples": count} for frames, count in
                        worker_stacks.get(id(item), Counter()).most_common(3)]}
                    for item in sorted(rows, key=lambda item: item["duration_ms"], reverse=True)[:20]],
                "main_thread_stacks": [{"frames": frames, "samples": count} for frames, count in
                    Counter(frames for stamp, frames in tuple(self.samples) if stamp >= start).most_common(8)]}


async def replay(client, body):
    start, first, count, usage = time.monotonic(), None, 0, None
    network = []

    async def trace(event, _info):
        # Trace metadata can contain authorization headers: only record event
        # names and relative times, never the supplied connection/request data.
        network.append({"event": event, "at_ms": (time.monotonic() - start) * 1000})

    async with client.stream("POST", "chat/completions", content=body,
                             extensions={"trace": trace}) as response:
        if response.is_error:
            # A failed replay is a provider observation, not a failed native
            # reply. Preserve it without exporting the provider's raw body.
            return {"first_content_ms": None, "duration_ms": (time.monotonic() - start) * 1000,
                    "content_chars": 0, "usage": None, "network_events": network,
                    "http_status": response.status_code, "passed": False}
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            value = json.loads(line[5:])
            if value.get("usage"):
                usage = value["usage"]
            for choice in value.get("choices", []):
                content = choice.get("delta", {}).get("content") or ""
                if content and first is None:
                    first = (time.monotonic() - start) * 1000
                count += len(content)
    return {"first_content_ms": first, "duration_ms": (time.monotonic() - start) * 1000,
            "content_chars": count, "usage": usage, "network_events": network,
            "http_status": response.status_code, "passed": True}


async def terminal(reader, after, source="conversation"):
    async with asyncio.timeout(120):
        async for _ in reader.follow_heads():
            for message in await reader.read_async(lambda current: current.snapshot(after_seq=after)):
                if message.source != source:
                    continue
                if isinstance(message.body, Control):
                    raise RuntimeError("controlled reply ended with " + message.body.action)  # noqa: TRY004
                if isinstance(message.body, Output) and message.body.finish == "complete":
                    return message
    raise RuntimeError("reply stream closed")


async def evaluate(args):
    if args.workspace.exists():
        raise ValueError("use a new workspace; do not attach this fixture to production")
    telegram = args.telegram_token_file is not None
    if telegram != (args.telegram_chat_file is not None):
        raise ValueError("Telegram mode requires both private token and chat files")
    model = EvaluationModel.read(args.model_config)
    if model.driver_id not in {"openai-compatible", "step-plan"}:
        raise ValueError("exact wire replay supports these two drivers only")
    os.environ["AKASHIC_TIMING"] = "1"
    os.environ["AKASHIC_PLUGIN_HOME"] = str(args.workspace / "isolated-plugin-home")
    initialize_workspace(args.workspace)
    profile_source = getattr(args, "memory_documents", None)
    profile_digests = {}
    if profile_source is not None:
        profile_source = profile_source.resolve()
        if not profile_source.is_dir():
            raise ValueError("memory documents must be an existing directory")
        for path in profile_source.rglob("*.md"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(profile_source)
            destination = args.workspace / "memory" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = path.read_bytes()
            destination.write_bytes(payload)
            profile_digests[str(relative)] = hashlib.sha256(payload).hexdigest()
    extension = args.workspace / "fixtures"
    shutil.copytree(ROOT / "eval/fixtures/alignment", extension / "alignment",
                    ignore=shutil.ignore_patterns("__pycache__"))
    report = {"implementation": source_identity(), "model_config": model.description(),
              "scope": "Isolated native graph; same configured memory and tools. Exact final native payload replayed directly after its native run; native-first order may warm provider cache. These are observations, not randomized A/B speedups.",
              "telegram": telegram, "cases": []}
    if profile_source is not None:
        report["memory_document_sha256"] = profile_digests
        report["memory_document_scope"] = "Markdown snapshots only; no production graph, credentials, sessions or polling copied."
    for name in ("eval/telegram_latency.py", "plugins/reply_program/program.py", "plugins/telegram_sender/sender.py",
                 "plugins/compaction/plugin.py", "plugins/compaction/message_summary.py", "plugins/openai_compatible/driver.py",
                 "plugins/openai_compatible/plugin.py", "core/net/http.py", "plugins/ledger/log.py",
                 "plugins/conversation/plugin.py", "plugins/models/projection.py", "plugins/context/api.py",
                 "plugins/akasha/runtime.py", "plugins/telegram_channel/drafts.py", "plugins/context/materials.py"):
        report["implementation"][name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    if telegram:
        from agent.plugin_composition.config_input import save_config, save_credential
        from agent.plugins.bundles import set_plugin_choice

        target = int(args.telegram_chat_file.read_text().strip())
        if target <= 0:
            raise ValueError("only an explicitly selected private chat is allowed")
        for name in ("telegram_channel", "telegram_sender"):
            data = args.workspace / ("plugin-data/" + name + "-builtin")
            ref = save_credential(data, args.telegram_token_file.read_text().strip())
            save_config(data, {"enabled": True, "token": ref})
            set_plugin_choice(args.workspace, name, enabled=True)
        report["telegram_scope"] = "Synthetic Update enters actual adapter/Core ingress. Real bot draft and final send APIs, actual native delivery receipt. No getUpdates, user-device or network-to-poller latency claim. Chat IDs and token URLs excluded."

    capture, timings, file_io = Capture(), Timings(), FileIoCapture()
    logger = logging.getLogger("akashic.timing")
    logger.addHandler(timings)
    logger.setLevel(logging.INFO)

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    active_case = None
    try:
        with ExitStack() as stack:
            if telegram:
                from telegram.ext import Updater
                stack.enter_context(patch.object(Updater, "start_polling", AsyncMock()))
            async with provider_slot(model, args.key_file), runtime(args.workspace, extensions=(extension,)) as core:
                await model.configure(core, args.key_file)
                await configure_embedding(core, args.embedding_manifest)
                context = core.plugin_manager.live_root.context
                catalog = context.require(MESSAGE_CATALOG)
                capture.install(stack)
                if getattr(args, "file_io_trace", False):
                    file_io.install(stack)
                if telegram:
                    channels = context.require(CHANNELS)
                    bindings = [b for b in channels._bindings.values() if b.channel_name == "telegram" and b.started]
                    if len(bindings) != 1:
                        raise RuntimeError("controlled Telegram binding not ready")
                    adapter = bindings[0].adapter
                    bot = adapter._app.bot
                    identity = await bot.get_me()
                    if args.expected_bot and identity.username != args.expected_bot:
                        raise ValueError("bot does not match expected identity")
                    original_draft = type(bot).send_message_draft
                    draft_rows = []

                    async def draft(bot, **values):
                        begin = time.monotonic()
                        result = await original_draft(bot, **values)
                        draft_rows.append({"begin": begin, "ack": time.monotonic(), "chars": len(values["text"])})
                        return result

                    stack.enter_context(patch.object(type(bot), "send_message_draft", draft))
                    session = "telegram:" + str(target)
                    await rpc(core, "evaluation/seed", {"session_id": session})
                headers = {"Authorization": "Bearer " + args.key_file.read_text().strip(), "Content-Type": "application/json"}
                async with await AsyncClient.for_workspace(args.workspace) as client, httpx.AsyncClient(
                    base_url=model.endpoint.rstrip("/") + "/", headers=headers, timeout=120) as direct_client:
                    cases = [("short-cold", "这是速度检查，只回答‘好的’，不要调用工具。"),
                             ("short-warm", "这是速度检查，只回答‘收到’，不要调用工具。"),
                             ("model-identity", "你现在底座啥模型？"),
                             ("long-stream", "直接用约400个汉字解释为什么人们喜欢星空。不要调用工具。")]
                    selected = getattr(args, "case_names", None)
                    if selected:
                        available = dict(cases + [("natural-greeting", "你好"), ("natural-followup", "在吗")])
                        available.update({label: "根据前面的项目记录，回答项目代号、当前截止日期、负责人、演示数据限制，以及仍未完成的工作。用一行简答，不调用工具。"
                                          for label in ("context-compaction", "context-maintenance")})
                        cases = [(label, available[label]) for label in dict.fromkeys(selected)]
                    for label, prompt in cases:
                        if not telegram:
                            session = "programmatic:latency-" + uuid4().hex
                            await client.request("programmatic/session/admit", {"session_id": session, "persist_memory": False})
                        if label in {"context-compaction", "context-maintenance"}:
                            await rpc(core, "evaluation/seed", {"session_id": session,
                                "visibility": "listed" if telegram else "internal", "records": [
                                {"kind": "input", "text": "项目记录：代号 KIRA-C27，截止日期由2026-11-03更正为2026-11-06，负责人许澄。演示必须用匿名样本。仍未完成：重启后核对投递回执。"},
                                {"kind": "output", "text": "重复检查已完成，无新增事实。" * (7000 if label == "context-compaction" else 3000)},
                                {"kind": "output", "text": "OPEN_COMPACTION_WORK", "source": "job:evaluation", "finish": "continue"},
                            ]})
                        reader = catalog.reader(session)
                        original = await reader.read_async(lambda current: current.snapshot()) if label in {"context-compaction", "context-maintenance"} else ()
                        head, call_start, timing_start = await reader.head_async(), len(capture.calls), len(timings.rows)
                        draft_start = len(draft_rows) if telegram else 0
                        identity = "probe-" + uuid4().hex
                        start = time.monotonic()
                        active_case = {"case": label, "start": start, "session": session,
                                       "call_start": call_start, "timing_start": timing_start}
                        if telegram:
                            message = SimpleNamespace(message_id=identity, text=prompt, caption=None, photo=(),
                                document=None, reply_to_message=None, date=datetime.now(UTC))
                            update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(
                                id=target, username=None), effective_chat=SimpleNamespace(id=target))
                            await adapter._on_update(update, SimpleNamespace(bot=bot))
                            answer = await terminal(reader, head)
                            delivery = context.require(DELIVERY_READ)
                            async with asyncio.timeout(30):
                                while not (status := await run_file_io(lambda: delivery.status(answer.message_id, "telegram"))) or not status.get("receipt"):
                                    await asyncio.sleep(0.01)
                            receipt = status["receipt"]
                            if receipt.get("status") != "delivered":
                                raise RuntimeError("controlled Telegram delivery failed")
                        else:
                            await client.request("programmatic/message/send", {"session_id": session, "message_id": identity, "text": prompt})
                            answer = await terminal(reader, head, source="programmatic")
                        total = (time.monotonic() - start) * 1000
                        maintenance_wait = None
                        if label == "context-maintenance":
                            begin_wait = time.monotonic()
                            summaries = context.require(COMPACTION_SUMMARIES)
                            async with asyncio.timeout(120):
                                while await run_file_io(lambda: summaries.head(session)) is None:
                                    await asyncio.sleep(0.05)
                            maintenance_wait = (time.monotonic() - begin_wait) * 1000
                        calls = capture.calls[call_start:]
                        if not calls:
                            raise RuntimeError("native wire capture was not attached")
                        body = next(c["body"] for c in reversed(calls) if c["purpose"] == "reply")
                        wire = json.loads(body)
                        role_chars = {}
                        for message in wire["messages"]:
                            role = message["role"]
                            role_chars[role] = role_chars.get(role, 0) + len(json.dumps(
                                message.get("content"), ensure_ascii=False))
                        rows = await reader.read_async(lambda current: current.snapshot(after_seq=head))
                        first = next((c["first_content_at"] for c in calls if c["first_content_at"] is not None), None)
                        row = {"case": label, "passed": True, "native_total_ms": total,
                               "model_calls": len(calls), "tool_results": sum(isinstance(m.body, ToolResult) for m in rows),
                               "native_driver_ms": sum(c["duration_ms"] for c in calls),
                               "native_reply_driver_ms": sum(c["duration_ms"] for c in calls if c["purpose"] == "reply"),
                               "native_summary_driver_ms": sum(c["duration_ms"] for c in calls if c["purpose"] == "summary"),
                               "native_first_visible_content_ms": None if first is None else (first - start) * 1000,
                               "request_sha256": hashlib.sha256(body).hexdigest(), "request_bytes": len(body),
                               "request_profile": {"content_chars_by_role": role_chars,
                                   "reasoning_chars": sum(len(m.get("reasoning_content") or "") for m in wire["messages"]),
                                   "tool_schema_chars": len(json.dumps(wire.get("tools", []), ensure_ascii=False)),
                                   "tool_schema_count": len(wire.get("tools", []))},
                               "same_payload_replayed": not getattr(args, "no_direct_replay", False),
                               "direct": None if getattr(args, "no_direct_replay", False) else await replay(direct_client, body)}
                        row["native_attempts"] = [{k: v for k, v in c.items() if k in (
                            "purpose", "duration_ms", "legacy_think_tag_seen", "thinking_chars", "usage")}
                            | {"begin_ms": (c["begin"] - start) * 1000}
                            | {"request_policy": {k: json.loads(c["body"])[k] for k in
                                ("max_tokens", "reasoning_effort", "thinking") if k in json.loads(c["body"])}}
                            | {k + "_ms": (c[k] - start) * 1000 for k in (
                                "first_raw_content_at", "legacy_close_at", "first_content_at") if c.get(k) is not None}
                            for c in calls]
                        if label == "model-identity":
                            row["bound_model_named"] = model.model in text(answer)
                            row["passed"] = row["bound_model_named"] and row["tool_results"] == 0
                        if label in {"natural-greeting", "natural-followup"}:
                            row["passed"] = bool(text(answer).strip()) and row["tool_results"] == 0
                            row["prompt_forbids_tools"] = False
                        if label in {"context-compaction", "context-maintenance"}:
                            summaries = context.require(COMPACTION_SUMMARIES)
                            summary = await run_file_io(lambda: summaries.head(session))
                            facts = ("KIRA-C27", "2026-11-06", "许澄", "匿名", "投递回执")
                            checks = {
                                "summary_created": summary is not None,
                                "answer_retains_facts": all(fact in text(answer) for fact in facts),
                                "raw_history_unchanged": (await reader.read_async(lambda current: current.snapshot()))[:len(original)] == original,
                                "open_work_not_compacted": summary is not None and not any(
                                    identity.endswith(":seed:2") for identity in summary.source_message_ids),
                            }
                            row["compaction"] = {**checks, "facts_checked": len(facts),
                                "tokens_before": None if summary is None else summary.tokens_before,
                                "tokens_after": None if summary is None else summary.tokens_after}
                            if label == "context-maintenance":
                                reply_end = max(c["begin"] + c["duration_ms"] / 1000 for c in calls if c["purpose"] == "reply")
                                row["compaction"].update(maintenance_wait_after_reply_ms=maintenance_wait,
                                    measurement_scope="native_total_ms stops at reply/delivery; model_calls and native_driver_ms also include subsequent maintenance",
                                    summary_started_after_reply_model=all(c["begin"] >= reply_end for c in calls if c["purpose"] == "summary"))
                                checks["summary_started_after_reply_model"] = row["compaction"]["summary_started_after_reply_model"]
                            row["passed"] = all(checks.values()) and row["tool_results"] == 0
                        if telegram:
                            drafts = draft_rows[draft_start:]
                            row.update(drafts=len(drafts), final_delivered=True,
                                first_draft_ack_ms=None if not drafts else (drafts[0]["ack"] - start) * 1000,
                                draft_network_ms=[(d["ack"] - d["begin"]) * 1000 for d in drafts])
                        row["timings"] = [{k: v for k, v in r.items() if k in (
                            "phase", "operation_id", "measurement_unit", "measurement_value", "counts")}
                            for r in timings.rows[timing_start:] if r.get("session_id") == session]
                        if getattr(args, "file_io_trace", False):
                            row["file_io"] = file_io.summary(start)
                        report["cases"].append(row)
                        active_case = None
                        save()
                        print(json.dumps({k: row[k] for k in ("case", "passed", "native_total_ms", "tool_results")}), flush=True)
        report["shutdown_completed"] = True
        report["passed"] = len(report["cases"]) == len(cases) and all(row["passed"] for row in report["cases"])
    except Exception as error:
        report.update(passed=False, failure_type=type(error).__name__)
        if active_case is not None:
            calls = capture.calls[active_case["call_start"]:]
            row = {"case": active_case["case"], "passed": False, "failure_type": type(error).__name__,
                   "native_total_ms": (time.monotonic() - active_case["start"]) * 1000,
                   "native_driver_ms": sum(call["duration_ms"] for call in calls),
                   "native_attempts": [{key: value for key, value in call.items() if key in {
                       "duration_ms", "purpose", "failed", "error_type", "http_status"}} for call in calls],
                   "timings": [{key: value for key, value in entry.items() if key in {
                       "phase", "operation_id", "measurement_unit", "measurement_value", "counts"}}
                       for entry in timings.rows[active_case["timing_start"]:] if entry.get("session_id") == active_case["session"]]}
            if getattr(args, "file_io_trace", False):
                row["file_io"] = file_io.summary(active_case["start"])
            report["cases"].append(row)
        raise
    finally:
        logger.removeHandler(timings)
        save()
    return report.get("passed", False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "key-file", "model-config", "embedding-manifest", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--telegram-token-file", type=Path)
    parser.add_argument("--telegram-chat-file", type=Path)
    parser.add_argument("--expected-bot")
    parser.add_argument("--case", dest="case_names", action="append", choices=(
        "natural-greeting", "natural-followup", "context-compaction", "context-maintenance", "short-cold", "short-warm", "model-identity", "long-stream"))
    parser.add_argument("--no-direct-replay", action="store_true",
                        help="Measure only native turns without an intervening duplicate provider request")
    parser.add_argument("--memory-documents", type=Path,
                        help="Read-only Markdown snapshots copied into the isolated workspace")
    parser.add_argument("--file-io-trace", action="store_true",
                        help="Observe worker queue and work time; no operation arguments or message bodies")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(evaluate(args)) else 1)


if __name__ == "__main__":
    main()
