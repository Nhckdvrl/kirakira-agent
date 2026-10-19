"""Real Step5 main-chain probes: production pipeline, isolated workspaces only.

This measures end-to-end and provider timings separately. Fault injections never
replace model responses. Reports are incremental, immutable and include failures.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from agent.model_runtime.types import RetryableModelError
from bootstrap.runtime import build_runtime
from bus.events_lifecycle import ModelAttemptStatus
from core.schema import ToolSpec
from infra.providers.step_plan import STEP5_MODEL, STEP_PLAN_BASE_URL, StepPlanClient
from scripts.verify_scheduler_step_plan import summary


class ObservedClient:
    def __init__(self, client):
        self.client = client
        self.requests = []
        self.before = None
        self.first_delta = asyncio.Event()

    def complete_stream(self, *args, **kwargs):
        raise AssertionError("verification uses native async streaming")

    async def _call(self, streaming, args, kwargs):
        started = time.perf_counter()
        row = {"status": "started", "streaming": streaming}
        self.requests.append(row)
        try:
            if self.before is not None:
                self.before()
            if streaming:
                callback = args[5]

                def delta(content, reasoning):
                    if content or reasoning:
                        row.setdefault(
                            "first_delta_seconds", time.perf_counter() - started
                        )
                        self.first_delta.set()
                    callback(content, reasoning)

                response = await self.client.acomplete_stream(
                    *args[:5], delta, **kwargs
                )
            else:
                response = await self.client.acomplete(*args, **kwargs)
            row.update(
                status="completed",
                usage=response.usage,
                stop_reason=response.stop_reason,
                tools=[call.name for call in response.tool_calls],
            )
            return response
        except BaseException as exc:
            row.update(
                status=type(exc).__name__, http_status=getattr(exc, "status_code", None)
            )
            if getattr(exc, "verification_injected", False):
                row["injected"] = True
            raise
        finally:
            row["seconds"] = time.perf_counter() - started

    async def acomplete(self, *args, **kwargs):
        return await self._call(False, args, kwargs)

    async def acomplete_stream(self, *args, **kwargs):
        return await self._call(True, args, kwargs)


async def verify(args):
    secret = args.key_file.read_text().strip()
    args.workspace.mkdir(parents=True, exist_ok=False)
    if args.output.exists():
        raise ValueError("use a fresh output; previous results are immutable")
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
        "billing_surface": "step_plan",
        "reasoning_effort": "medium",
        "python": platform.python_version(),
        "repeats": args.repeats,
        "concurrency": args.concurrency,
        "streaming": True,
        "fixture_type": "synthetic functional main-chain contracts",
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__).resolve().relative_to(Path.cwd().resolve()),
                *sorted(Path("agent/core").glob("*.py")),
                Path("agent/tools/registry.py"),
                Path("utils/async_tasks.py"),
            ]
        },
        "limitations": [
            "not an external task benchmark",
            "small latency sample; no provider speedup claim",
            "unknown external side effects are not automatically replayed",
            "cancelled streams may have unreported provider usage",
        ],
        "cases": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def publish():
        report["summary"] = summary(report["cases"])
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    @asynccontextmanager
    async def rig(work, observed, *, keep_files=False):
        work.mkdir(parents=True, exist_ok=True)
        config = work / "config.toml"
        if not config.exists():
            config.write_text(
                '[llm.main]\nmodel="step-5-preview"\nbase_url="https://api.stepfun.com/step_plan/v1"\n'
                'reasoning_effort="medium"\n[agent]\nmax_iterations=12\nmax_tokens=8192\n[memory]\nenabled=false\n'
            )
        runtime = await build_runtime(work)
        runtime.reasoner.model_client = observed
        runtime.reasoner._model_io.model_client = observed
        for name in runtime.tools.names():
            if not keep_files or name not in {"read_file", "write_file", "list_dir"}:
                runtime.tools.unregister(name)
        try:
            yield runtime
        finally:
            await runtime.stop_background([])

    limit = asyncio.Semaphore(args.concurrency)

    async def case(category, repetition):
        async with limit:
            started = time.perf_counter()
            observed = ObservedClient(client)
            row = {"category": category, "repeat": repetition, "status": "started"}
            work = args.workspace / f"{category}-{repetition}"
            nonce = "KIRA_" + uuid4().hex[:12]
            evidence = {}
            try:
                async with rig(
                    work, observed, keep_files=category == "files_restart"
                ) as runtime:

                    async def ask(prompt, key="verify:main"):
                        return await runtime.process_direct(
                            prompt,
                            session_key=key,
                            skip_memory_retrieval=True,
                            skip_post_memory=True,
                        )

                    def register(
                        name, description, properties, required, handler, **options
                    ):
                        runtime.tools.register(
                            ToolSpec(
                                name,
                                description,
                                {
                                    "type": "object",
                                    "properties": properties,
                                    "required": required,
                                    "additionalProperties": False,
                                },
                            ),
                            handler,
                            **options,
                        )

                    if category == "parallel_reads":
                        active, peak = 0, 0
                        reads, writes = [], []
                        values = {i: (repetition + 3) * 100 + i * 7 for i in range(8)}

                        async def lookup(index):
                            nonlocal active, peak
                            active += 1
                            peak = max(peak, active)
                            reads.append(index)
                            try:
                                await asyncio.sleep(0.06)
                                return json.dumps(
                                    {"index": index, "value": values[index]}
                                )
                            finally:
                                active -= 1

                        def save(total):
                            assert active == 0 and total == sum(values.values())
                            writes.append(total)
                            return f"saved {total}"

                        register(
                            "lookup",
                            "Independent read by index, safe to call in parallel.",
                            {"index": {"type": "integer", "enum": list(values)}},
                            ["index"],
                            lookup,
                            parallel_safe=True,
                        )
                        register(
                            "save",
                            "Save the final total exactly once after all reads.",
                            {"total": {"type": "integer"}},
                            ["total"],
                            save,
                            risk="write",
                        )
                        result = await ask(
                            "Look up all indices 0 through 7. Issue all eight independent lookup calls in ONE response, then sum the returned values and save the total exactly once. Finally report the total."
                        )
                        assert sorted(reads) == list(values), f"reads={reads}"
                        assert writes == [sum(values.values())], f"writes={writes}"
                        assert 2 <= peak <= 4, (
                            f"native model did not exercise bounded parallel calls: peak={peak}"
                        )
                        assert str(sum(values.values())) in result.content
                        evidence.update(peak_active=peak, reads=reads, writes=writes)

                    elif category == "retry_after_effect":
                        writes, statuses = [], []
                        injected = False

                        def write_once(code):
                            if code != nonce:
                                raise ValueError(
                                    "expected the exact literal opaque identifier, not program source"
                                )
                            writes.append(code)
                            (work / "effect.txt").write_text(code)
                            return "committed " + code

                        def fault():
                            nonlocal injected
                            if writes and not injected:
                                injected = True
                                error = RetryableModelError(
                                    "verification: generation failure after durable tool result",
                                    retry_after=0,
                                )
                                error.verification_injected = True
                                raise error

                        observed.before = fault
                        runtime.event_bus.on(
                            ModelAttemptStatus,
                            lambda event: statuses.append(event.phase),
                        )
                        register(
                            "write_once",
                            "Write the literal opaque identifier exactly once; do not generate program source.",
                            {"code": {"type": "string", "enum": [nonce]}},
                            ["code"],
                            write_once,
                            risk="write",
                        )
                        result = await ask(
                            f'Use write_once to write the literal opaque string "{nonce}" exactly once. The code argument is this exact identifier, not program source. Report the committed identifier after success.'
                        )
                        evidence.update(
                            effect_count=len(writes),
                            injected=injected,
                            recovery_statuses=statuses,
                            reply=result.content,
                        )
                        assert (
                            writes == [nonce] and injected and "recovered" in statuses
                        ), (
                            f"effect_count={len(writes)}, injected={injected}, phases={statuses}"
                        )
                        assert nonce in result.content, result.content

                    elif category == "deferred_search":
                        calls = []

                        def search(query):
                            calls.append("search")
                            return json.dumps(
                                {
                                    "unlocked": ["secret_lookup"],
                                    "description": "Call secret_lookup with code="
                                    + nonce,
                                }
                            )

                        def hidden(code):
                            assert code == nonce
                            calls.append("lookup")
                            return "RECEIPT_" + nonce

                        register(
                            "tool_search",
                            "Load deferred secret_lookup before using it.",
                            {"query": {"type": "string"}},
                            ["query"],
                            search,
                        )
                        register(
                            "secret_lookup",
                            "Retrieve the receipt for the exact code.",
                            {"code": {"type": "string"}},
                            ["code"],
                            hidden,
                            deferred=True,
                        )
                        result = await ask(
                            f"Load the deferred secret_lookup using tool_search, then retrieve the receipt for code {nonce}. Output the receipt."
                        )
                        assert calls == ["search", "lookup"], f"calls={calls}"
                        assert "RECEIPT_" + nonce in result.content
                        evidence.update(calls=calls)

                    elif category == "tool_error_repair":
                        calls = []

                        def read_version(version):
                            calls.append(version)
                            if version != "v2":
                                return "Error: v1 expired. Retry this tool with version v2."
                            return "CURRENT_" + nonce

                        register(
                            "read_version",
                            "Read a version; expired versions return repair instructions.",
                            {"version": {"type": "string"}},
                            ["version"],
                            read_version,
                            parallel_safe=True,
                        )
                        result = await ask(
                            "First call read_version with version v1. If the tool reports an expired version, follow its repair instruction and report the current value."
                        )
                        assert calls == ["v1", "v2"], f"calls={calls}"
                        assert "CURRENT_" + nonce in result.content
                        evidence.update(calls=calls)

                    elif category == "files_restart":
                        first = await ask(
                            f"Use write_file to write exactly {nonce} to probe.txt, then read_file to verify it. Report the exact content."
                        )
                        assert (work / "probe.txt").read_text().strip() == nonce
                        evidence["first_reply"] = first.content

                    elif category == "compaction_restart":
                        runtime.reasoner.config.context_window = 40_000
                        runtime.reasoner.config.history_window = 100_000
                        session = runtime.session_manager.get_or_create("verify:main")
                        for index in range(40):
                            padding = "\n".join(
                                f"Fixture unit {index}, observation {line}: local read-only background detail; no external effect is pending."
                                for line in range(60)
                            )
                            session.add_message(
                                "user",
                                f"Project verification code: {nonce}.\n{padding}",
                            )
                            session.add_message(
                                "assistant",
                                "Recorded the project code and background observations.",
                            )
                        runtime.session_manager.save(session)
                        original = json.dumps(session.messages, sort_keys=True)
                        result = await ask(
                            "What is the project verification code? Reply with the exact code."
                        )
                        assert nonce in result.content
                        active = runtime.session_manager.get_active_compaction(
                            "verify:main"
                        )
                        assert active is not None and active.generation >= 1
                        assert len(session.messages) == 82
                        assert (
                            json.dumps(session.messages[:80], sort_keys=True)
                            == original
                        )
                        evidence.update(
                            generation=active.generation,
                            source_messages_preserved=80,
                            summary_sha256=hashlib.sha256(
                                active.summary.encode()
                            ).hexdigest(),
                            summary=active.summary,
                        )

                    elif category == "stream_cancel":
                        task = asyncio.create_task(
                            ask(
                                "Write a long detailed numbered list of at least 200 practical software testing scenarios. Start immediately."
                            )
                        )
                        try:
                            await asyncio.wait_for(observed.first_delta.wait(), 120)
                            at = time.perf_counter()
                            assert runtime.loop.request_interrupt("verify:main")
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                            else:
                                raise AssertionError(
                                    "stream completed before cancellation was exercised"
                                )
                            evidence["cancel_drain_ms"] = (
                                time.perf_counter() - at
                            ) * 1000
                            assert not runtime.pipeline.admission.is_busy("verify:main")
                            assert any(
                                request["status"] == "CancelledError"
                                for request in observed.requests
                            )
                            followup = await ask(f"Reply with exactly {nonce}.")
                            assert nonce in followup.content
                        finally:
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

                    else:
                        raise ValueError(category)

                    rows = runtime.reasoner.execution_store.recent(30)
                    assert all(
                        not runtime.reasoner.execution_store.unresolved(item["run_id"])
                        for item in rows
                    )
                    evidence["execution_statuses"] = [item["status"] for item in rows]
                if category == "files_restart":
                    async with rig(work, observed, keep_files=True) as restarted:
                        result = await restarted.process_direct(
                            "Read probe.txt and report its exact contents.",
                            session_key="verify:main",
                            skip_memory_retrieval=True,
                            skip_post_memory=True,
                        )
                        assert nonce in result.content
                        evidence["restart_reply"] = result.content
                        evidence["persisted_messages"] = len(
                            restarted.session_manager.get_or_create(
                                "verify:main"
                            ).messages
                        )
                elif category == "compaction_restart":
                    async with rig(work, observed) as restarted:
                        restarted.reasoner.config.context_window = 40_000
                        restarted.reasoner.config.history_window = 100_000
                        active = restarted.session_manager.get_active_compaction(
                            "verify:main"
                        )
                        assert (
                            active is not None
                            and active.generation == evidence["generation"]
                        )
                        assert (
                            hashlib.sha256(active.summary.encode()).hexdigest()
                            == evidence["summary_sha256"]
                        )
                        result = await restarted.process_direct(
                            "Repeat the exact project verification code.",
                            session_key="verify:main",
                            skip_memory_retrieval=True,
                            skip_post_memory=True,
                        )
                        assert nonce in result.content
                        evidence["restart_reply"] = result.content
                        evidence["persisted_messages"] = len(
                            restarted.session_manager.get_or_create(
                                "verify:main"
                            ).messages
                        )
                        assert evidence["persisted_messages"] == 84
                row.update(status="passed", evidence=evidence)
            except Exception as exc:
                row.update(
                    status="failed",
                    error=type(exc).__name__,
                    detail=str(exc).replace(secret, "[redacted]")[:1000],
                    evidence=evidence,
                )
            finally:
                row["seconds"] = time.perf_counter() - started
                row["requests"] = observed.requests
                report["cases"].append(row)
                publish()
                print(
                    json.dumps(
                        {
                            "category": category,
                            "repeat": repetition,
                            "status": row["status"],
                        }
                    ),
                    flush=True,
                )

    categories = args.categories or (
        "parallel_reads",
        "retry_after_effect",
        "deferred_search",
        "tool_error_repair",
        "files_restart",
        "stream_cancel",
        "compaction_restart",
    )
    await asyncio.gather(
        *(
            case(category, repetition)
            for repetition in range(args.repeats)
            for category in categories
        )
    )
    publish()
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if report["summary"]["passed"] != report["summary"]["total"]:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=(
            "parallel_reads",
            "retry_after_effect",
            "deferred_search",
            "tool_error_repair",
            "files_restart",
            "stream_cancel",
            "compaction_restart",
        ),
    )
    args = parser.parse_args()
    if args.repeats < 1 or args.concurrency < 1:
        parser.error("repeats and concurrency must be positive")
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
