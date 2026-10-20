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

from bootstrap.runtime import build_runtime
from bus.events_lifecycle import ModelAttemptStatus
from core.schema import ToolSpec
from infra.providers.step_plan import STEP5_MODEL, STEP_PLAN_BASE_URL, StepPlanClient
from plugins.models.types import RetryableModelError
from eval.scenarios.scheduler import summary


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
                *[
                    path
                    for owner in (
                        "agent/plugin_composition",
                        "agent/composition",
                        "bootstrap",
                        "plugins/reply_program",
                        "plugins/react",
                        "plugins/models",
                        "plugins/sources",
                        "plugins/conversation",
                        "plugins/delivery",
                        "plugins/ledger",
                        "plugins/turn_projection",
                        "plugins/jobs",
                        "plugins/subagent",
                    )
                    for path in sorted(Path(owner).glob("*.py"))
                ],
                Path("plugins/tools/registry.py"),
                Path("utils/async_tasks.py"),
                Path("core/common/frozen_json.py"),
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
                    work,
                    observed,
                    keep_files=category in {"files_restart", "background_spawn"},
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

                    if category == "steer_job_isolation":
                        from bus.events import InboundMessage
                        from plugins.control.client import ControlClient
                        from plugins.ledger.message import Input

                        ready, release = asyncio.Event(), asyncio.Event()
                        job_started, follow_done = asyncio.Event(), asyncio.Event()
                        original_hold = runtime.pipeline.admission.hold
                        admissions = 0
                        human, job_code = "HUMAN_" + nonce, "JOB_" + nonce

                        @asynccontextmanager
                        async def hold(key):
                            nonlocal admissions
                            admissions += 1
                            first = admissions == 1
                            async with original_hold(key):
                                yield
                            if first:
                                await job_started.wait()

                        class GatedClient:
                            calls = 0
                            human_answer = None

                            async def call(self, streaming, args, kwargs):
                                self.calls += 1
                                current = self.calls
                                if current == 2:
                                    assert human not in json.dumps(args[0])
                                    job_started.set()
                                response = await (
                                    observed.acomplete_stream(*args, **kwargs)
                                    if streaming
                                    else observed.acomplete(*args, **kwargs)
                                )
                                if current == 1:
                                    ready.set()
                                    await release.wait()
                                if current == 3:
                                    assert human in response.text
                                    self.human_answer = response.text
                                    follow_done.set()
                                return response

                            async def acomplete(self, *args, **kwargs):
                                return await self.call(False, args, kwargs)

                            async def acomplete_stream(self, *args, **kwargs):
                                return await self.call(True, args, kwargs)

                        gated = GatedClient()
                        runtime.reasoner._model_io.model_client = gated
                        runtime.pipeline.admission.hold = hold
                        await runtime.control_server.start()
                        steer_client = await ControlClient.connect(
                            str(runtime.control_server.endpoint)
                        )
                        first = asyncio.create_task(
                            ask("Reply with exactly FIRST_" + nonce)
                        )
                        job = None
                        try:
                            await asyncio.wait_for(ready.wait(), 90)
                            receipt = await steer_client.request(
                                "turn/steer",
                                {
                                    "threadId": "verify:main",
                                    "messageId": "isolated-" + nonce,
                                    "input": "Reply with exactly " + human,
                                },
                            )
                            assert receipt["staged"]
                            job = asyncio.create_task(
                                runtime.pipeline.run(
                                    InboundMessage(
                                        "verify",
                                        "job",
                                        "main",
                                        "Reply with exactly " + job_code,
                                        metadata={
                                            "source": "job:fixture",
                                            "omit_user_turn": True,
                                            "skip_post_memory": True,
                                        },
                                    ),
                                    "verify:main",
                                    dispatch_outbound=False,
                                )
                            )
                            async with asyncio.timeout(10):
                                while (
                                    len(
                                        runtime.pipeline.programs.store.list(
                                            "verify:main"
                                        )
                                    )
                                    < 2
                                ):
                                    await asyncio.sleep(0)
                            release.set()
                            await asyncio.wait_for(first, 90)
                            result = await asyncio.wait_for(job, 90)
                            assert (
                                job_code in result.content
                                and human not in result.content
                            )
                            await asyncio.wait_for(follow_done.wait(), 90)
                            async with original_hold("verify:main"):
                                messages = runtime.message_log.read("verify:main")
                                inputs = [
                                    m
                                    for m in messages
                                    if isinstance(m.body, Input)
                                    and any(
                                        p.kind == "text" and human in str(p.value)
                                        for p in m.body.parts
                                    )
                                ]
                                assert (
                                    len(inputs) == 1
                                    and inputs[0].source == "conversation"
                                )
                                assert (
                                    len(
                                        runtime.pipeline.programs.store.list(
                                            "verify:main"
                                        )
                                    )
                                    == 3
                                )
                                assert gated.calls == 3
                                evidence.update(
                                    user_input_source=inputs[0].source,
                                    programs=3,
                                    job_reply=result.content,
                                    human_reply=gated.human_answer,
                                    calls=gated.calls,
                                )
                        finally:
                            release.set()
                            job_started.set()
                            if not first.done():
                                first.cancel()
                            if job is not None and not job.done():
                                job.cancel()
                            await asyncio.gather(
                                first, *([job] if job else []), return_exceptions=True
                            )
                            await steer_client.close()
                            runtime.pipeline.admission.hold = original_hold

                    elif category in {"steer_next_step", "steer_resume"}:
                        from plugins.control.client import ControlClient
                        from plugins.ledger.message import Input
                        from plugins.turn_projection.plugin import TurnProjection

                        effects = []
                        staged = asyncio.Event()
                        await runtime.control_server.start()
                        steer_client = await ControlClient.connect(
                            str(runtime.control_server.endpoint)
                        )
                        steer_spec = ToolSpec(
                            "read_code",
                            "Fetch the verification code once",
                            {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        )

                        async def read_code():
                            effects.append(nonce)
                            observed.first_delta.clear()
                            for identity, text in (
                                ("drop-" + nonce, "Answer WITHDRAWN_CODE"),
                                (
                                    "keep-" + nonce,
                                    "Update: the tool is done. Reply with exactly "
                                    + nonce
                                    + ". Do not call any more tools.",
                                ),
                            ):
                                receipt = await steer_client.request(
                                    "turn/steer",
                                    {
                                        "threadId": "verify:main",
                                        "messageId": identity,
                                        "input": text,
                                    },
                                )
                                assert (
                                    receipt["staged"]
                                    and receipt["messageId"] == identity
                                )
                            assert (
                                await steer_client.request(
                                    "turn/withdraw",
                                    {
                                        "threadId": "verify:main",
                                        "messageId": "drop-" + nonce,
                                    },
                                )
                            )["withdrawn"]
                            staged.set()
                            return nonce

                        runtime.tools.register(steer_spec, read_code)
                        prompt = "First call read_code exactly once. Then answer ORIGINAL_CODE unless a later user input changes the answer format."
                        task = asyncio.create_task(ask(prompt))
                        try:
                            await asyncio.wait_for(staged.wait(), 90)
                            if category == "steer_resume":
                                await asyncio.wait_for(observed.first_delta.wait(), 90)
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass
                                else:
                                    raise AssertionError(
                                        "reply completed before interruption"
                                    )
                            else:
                                result = await task
                                assert (
                                    nonce in result.content
                                    and "ORIGINAL_CODE" not in result.content
                                )
                            programs = runtime.pipeline.programs.store.list(
                                "verify:main"
                            )
                            assert (
                                len(programs) == 1
                                and len(programs[0]["steer_ids"]) == 1
                            )
                            assert effects == [nonce]
                            messages = runtime.session_manager.message_log.read(
                                "verify:main"
                            )
                            assert (
                                sum(
                                    isinstance(message.body, Input)
                                    for message in messages
                                )
                                == 2
                            )
                            turns = TurnProjection().project(messages, "conversation")
                            assert len(turns) == 1
                            assert turns[0].status == (
                                "open" if category == "steer_resume" else "complete"
                            )
                            assert all(
                                "WITHDRAWN_CODE" not in str(message.body)
                                for message in messages
                            )
                            evidence.update(
                                program_id=programs[0]["id"],
                                effects=len(effects),
                                ledger_messages=len(messages),
                                turn_status=turns[0].status,
                            )
                        finally:
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

                            await steer_client.close()

                    elif category == "parallel_reads":
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

                    elif category == "provider_reload":
                        from agent.composition import Module
                        from plugins.models.contract import (
                            MEMORY_MODEL,
                            MODEL,
                            MODEL_ID,
                        )
                        from plugins.sources.contract import ADMISSION

                        await runtime.start_background(start_channels=False)
                        ledger, previous = runtime.session_manager, runtime.pipeline
                        admission = runtime.composition.services.resolve(ADMISSION)
                        first = await ask(f"Reply with exactly PRE_{nonce}.")
                        assert "PRE_" + nonce in first.content

                        async def replace_models(ctx):
                            await ctx.provide(MODEL, observed)
                            await ctx.provide(MEMORY_MODEL, observed)
                            await ctx.provide(MODEL_ID, STEP5_MODEL)

                        await runtime.composition.replace(
                            "models",
                            Module(
                                "models",
                                replace_models,
                                provides=(MODEL, MEMORY_MODEL, MODEL_ID),
                                config=runtime.composition._modules["models"].config,
                                native=True,
                            ),
                        )
                        assert runtime.session_manager is ledger
                        assert runtime.pipeline is not previous
                        assert (
                            runtime.composition.services.resolve(ADMISSION) is admission
                        )
                        second = await ask(f"Reply with exactly POST_{nonce}.")
                        assert "POST_" + nonce in second.content
                        messages = ledger.get_or_create("verify:main").messages
                        assert len(messages) == 4
                        evidence.update(
                            ledger_preserved=True,
                            admission_preserved=True,
                            conversation_replaced=True,
                            original_messages=4,
                        )

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

                    elif category in {
                        "job_busy",
                        "job_idle",
                        "job_restart",
                        "background_spawn",
                        "job_resume",
                        "job_failed",
                        "jobs_reload",
                    }:
                        jobs = runtime.pipeline.jobs

                        def publish_job():
                            runtime.session_manager.get_or_create("verify:main")
                            return jobs.publish(
                                "probe-" + nonce,
                                session_key="verify:main",
                                channel="direct",
                                chat_id="local",
                                label="background probe",
                                profile="research",
                                result={
                                    "status": "failed"
                                    if category == "job_failed"
                                    else "completed",
                                    "result": "FAILURE_" + nonce
                                    if category == "job_failed"
                                    else nonce,
                                },
                            )

                        if category == "jobs_reload":
                            publish_job()
                            ledger, previous = runtime.session_manager, jobs
                            await runtime.composition.replace(
                                "jobs", runtime.composition._modules["jobs"]
                            )
                            jobs = runtime.pipeline.jobs
                            assert (
                                jobs is not previous
                                and runtime.session_manager is ledger
                            )
                            assert runtime.subagents.jobs is jobs
                            previous.close()
                            assert all(
                                runtime.tools.get_tool(name).owner is not None
                                for name in [
                                    "spawn",
                                    "spawn_manage",
                                    "schedule",
                                    "list_schedules",
                                    "cancel_schedule",
                                ]
                            )
                            result = await ask(
                                "Report the exact verification code from the recorded background task result."
                            )
                            assert nonce in result.content
                            assert jobs.read("probe-" + nonce)["status"] == "consumed"
                            evidence.update(
                                ledger_preserved=True,
                                jobs_replaced=True,
                                tool_contributions_owned=True,
                                reply=result.content,
                            )
                        elif category == "job_busy":

                            def settle_background():
                                publish_job()
                                return "The background result was recorded; incorporate its verification code."

                            register(
                                "settle_background",
                                "Complete the pending background probe.",
                                {},
                                [],
                                settle_background,
                            )
                            result = await ask(
                                "Call settle_background, then report the exact verification code from the background result."
                            )
                            notice = jobs.read("probe-" + nonce)
                            assert nonce in result.content
                            assert notice["status"] == "consumed"
                            assert notice["program_id"] == result.metadata["program_id"]
                            count = len(observed.requests)
                            assert (
                                await runtime.pipeline.run(
                                    None, "verify:main", job_notice_id="probe-" + nonce
                                )
                                is None
                            )
                            assert len(observed.requests) == count
                            assert (
                                len(runtime.pipeline.programs.store.list("verify:main"))
                                == 1
                            )
                            evidence.update(
                                same_program=True,
                                duplicate_wake_requests=0,
                                reply=result.content,
                            )
                        elif category == "job_idle":
                            await ask("Reply ready for background task results.")
                            delivered = asyncio.Event()
                            received = []

                            async def receive(message):
                                received.append(message)
                                delivered.set()

                            runtime.bus.subscribe_outbound("direct", receive)
                            await runtime.start_background(start_channels=False)
                            publish_job()
                            await asyncio.wait_for(delivered.wait(), 180)
                            assert len(received) == 1 and nonce in received[0].content
                            messages = runtime.session_manager.get_or_create(
                                "verify:main"
                            ).messages
                            assert (
                                len([m for m in messages if m["role"] == "user"]) == 1
                            )
                            assert messages[-1]["source"] == "job:probe-" + nonce
                            assert jobs.read("probe-" + nonce)["status"] == "consumed"
                            evidence.update(
                                deliveries=1,
                                synthetic_user_messages=0,
                                reply=received[0].content,
                            )
                        elif category == "job_restart":
                            publish_job()
                            evidence["pending_before_restart"] = jobs.read(
                                "probe-" + nonce
                            )["status"]
                            request = jobs.wake_request("probe-" + nonce, "verify:main")
                            identity, _ = runtime.pipeline.programs.accept(
                                request, "verify:main"
                            )
                            evidence["accepted_program_before_restart"] = identity
                        elif category == "job_failed":
                            publish_job()
                            result = await ask(
                                "Report the background task status and its exact diagnostic. Do not claim a failed task succeeded."
                            )
                            assert "FAILURE_" + nonce in result.content
                            assert jobs.read("probe-" + nonce)["status"] == "consumed"
                            evidence.update(
                                failed_result_reported=True, reply=result.content
                            )
                        elif category == "job_resume":
                            publish_job()
                            task = asyncio.create_task(
                                ask(
                                    "Report the background verification code and explain how the recorded task result supports it."
                                )
                            )
                            try:
                                await asyncio.wait_for(observed.first_delta.wait(), 120)
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass
                                else:
                                    raise AssertionError(
                                        "stream finished before cancellation"
                                    )
                                notice = jobs.read("probe-" + nonce)
                                assert notice["status"] == "claimed"
                                evidence.update(
                                    program_id=notice["program_id"],
                                    claimed_before_restart=True,
                                )
                            finally:
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                        else:
                            runtime.subagents.register_tool()
                            (work / "child-code.txt").write_text(nonce)

                            async def await_background():
                                assert await runtime.subagents.wait(timeout=180)
                                return (
                                    "Background task settled; use its recorded result."
                                )

                            register(
                                "await_background",
                                "Wait for the background child to finish before responding.",
                                {},
                                [],
                                await_background,
                            )
                            result = await ask(
                                "Use spawn with mode=background and profile=research to ask a child to read child-code.txt "
                                "and return its exact code. Do not read the file yourself. Then call await_background "
                                "and report the code from the background result."
                            )
                            notices = jobs.list("verify:main")
                            assert (
                                len(notices) == 1 and notices[0]["status"] == "consumed"
                            )
                            assert "spawn" in result.metadata["tools_used"]
                            assert "await_background" in result.metadata["tools_used"]
                            assert "read_file" not in result.metadata["tools_used"]
                            assert nonce in result.content
                            assert nonce in notices[0]["fact"]["result"]["result"]
                            evidence.update(
                                real_child_model=True,
                                notice=notices[0],
                                reply=result.content,
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
                if category == "steer_resume":
                    async with rig(work, observed) as restarted:

                        def never_repeat():
                            raise AssertionError("completed tool was replayed")

                        restarted.tools.register(steer_spec, never_repeat)
                        result = await restarted.process_direct(
                            "",
                            session_key="verify:main",
                            resume_program_id=evidence["program_id"],
                        )
                        assert (
                            nonce in result.content
                            and "ORIGINAL_CODE" not in result.content
                        )
                        assert effects == [nonce]
                        turns = TurnProjection().project(
                            restarted.session_manager.message_log.read("verify:main"),
                            "conversation",
                        )
                        assert len(turns) == 1 and turns[0].status == "complete"
                        evidence.update(
                            resumed_program_id=result.metadata["program_id"],
                            final_turn_status=turns[0].status,
                            replayed_effects=0,
                        )
                elif category == "job_resume":
                    async with rig(work, observed) as restarted:
                        result = await restarted.process_direct(
                            "",
                            session_key="verify:main",
                            resume_program_id=evidence["program_id"],
                        )
                        assert nonce in result.content
                        notice = restarted.pipeline.jobs.read("probe-" + nonce)
                        assert notice["status"] == "consumed"
                        assert notice["program_id"] == evidence["program_id"]
                        evidence.update(
                            resume_reply=result.content, same_program_after_restart=True
                        )
                elif category == "job_restart":
                    async with rig(work, observed) as restarted:
                        delivered = asyncio.Event()
                        received = []

                        async def receive_recovered(message):
                            received.append(message)
                            delivered.set()

                        restarted.bus.subscribe_outbound("direct", receive_recovered)
                        await restarted.start_background(start_channels=False)
                        await asyncio.wait_for(delivered.wait(), 180)
                        assert len(received) == 1 and nonce in received[0].content
                        jobs = restarted.pipeline.jobs
                        notice = jobs.read("probe-" + nonce)
                        assert notice["status"] == "consumed"
                        assert (
                            notice["program_id"]
                            == evidence["accepted_program_before_restart"]
                        )
                        assert not jobs.publish(
                            "probe-" + nonce,
                            session_key="verify:main",
                            channel="direct",
                            chat_id="local",
                            label="background probe",
                            profile="research",
                            result={"status": "completed", "result": nonce},
                        )
                        evidence.update(
                            restart_reply=received[0].content,
                            consumed=True,
                            duplicate_publish=False,
                            same_accepted_program=True,
                        )
                elif category == "files_restart":
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
        "steer_job_isolation",
        "steer_next_step",
        "steer_resume",
        "parallel_reads",
        "retry_after_effect",
        "deferred_search",
        "tool_error_repair",
        "files_restart",
        "stream_cancel",
        "compaction_restart",
        "provider_reload",
        "job_busy",
        "job_idle",
        "job_restart",
        "background_spawn",
        "job_resume",
        "job_failed",
        "jobs_reload",
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
            "steer_job_isolation",
            "steer_next_step",
            "steer_resume",
            "parallel_reads",
            "retry_after_effect",
            "deferred_search",
            "tool_error_repair",
            "files_restart",
            "stream_cancel",
            "compaction_restart",
            "provider_reload",
            "job_busy",
            "job_idle",
            "job_restart",
            "background_spawn",
            "job_resume",
            "job_failed",
            "jobs_reload",
        ),
    )
    args = parser.parse_args()
    if args.repeats < 1 or args.concurrency < 1:
        parser.error("repeats and concurrency must be positive")
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
