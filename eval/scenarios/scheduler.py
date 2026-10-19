"""Production-runtime experiments, synthetic fixtures, real Step5 Token Plan.

Each case owns a new workspace; only local file/scheduler/fixture tools are
advertised. Faults are injected at explicit lifecycle boundaries, never by
replacing model responses. Failed rows remain in the incremental report.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from bootstrap.runtime import build_runtime
from bus.events import InboundMessage
from bus.events_lifecycle import TurnCommitted
from core.schema import ToolSpec
from infra.providers.step_plan import STEP5_MODEL, STEP_PLAN_BASE_URL, StepPlanClient
from plugins.ledger.turn_source import load_committed_turn_messages


class MeteredClient:
    def __init__(self, client):
        self.client = client
        self.requests = []
        self.inputs = []

    async def acomplete(self, *args, **kwargs):
        started = time.monotonic()
        self.inputs.append(json.dumps(args[0], ensure_ascii=False))
        row = {"status": "started"}
        self.requests.append(row)
        try:
            response = await self.client.acomplete(*args, **kwargs)
            row.update(
                status="completed",
                usage=response.usage,
                stop_reason=response.stop_reason,
                tools=[call.name for call in response.tool_calls],
            )
            return response
        except BaseException as exc:
            row.update(status=type(exc).__name__)
            raise
        finally:
            row["seconds"] = time.monotonic() - started


def percentile(values, fraction):
    return (
        sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]
        if values
        else None
    )


def summary(rows):
    requests = [request for row in rows for request in row.get("requests", [])]
    usage = {}
    for request in requests:
        for key, value in request.get("usage", {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    groups = {}
    for category in sorted({row["category"] for row in rows}):
        selected = [row for row in rows if row["category"] == category]
        groups[category] = {
            "passed": sum(row["status"] == "passed" for row in selected),
            "total": len(selected),
        }
    return {
        "passed": sum(row["status"] == "passed" for row in rows),
        "total": len(rows),
        "groups": groups,
        "model_requests": len(requests),
        "completed_requests": sum(row["status"] == "completed" for row in requests),
        "usage": usage,
        "case_latency_seconds": {
            "p50": percentile([row["seconds"] for row in rows], 0.5),
            "p95": percentile([row["seconds"] for row in rows], 0.95),
        },
        "model_latency_seconds": {
            "p50": percentile([row["seconds"] for row in requests], 0.5),
            "p95": percentile([row["seconds"] for row in requests], 0.95),
        },
    }


async def verify(args):
    secret = args.key_file.read_text().strip()
    for key in (
        "LIGHT_MODEL_ID",
        "LIGHT_OPENAI_COMPATIBLE_BASE_URL",
        "LIGHT_OPENAI_COMPATIBLE_API_KEY",
    ):
        os.environ.pop(key, None)
    os.environ.update(
        MODEL_ID=STEP5_MODEL,
        OPENAI_COMPATIBLE_BASE_URL=STEP_PLAN_BASE_URL,
        OPENAI_COMPATIBLE_API_KEY=secret,
        AGENT_STREAM="false",
    )
    args.workspace.mkdir(parents=True, exist_ok=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError(
            "choose a new output; previous experiment results are immutable"
        )
    report = {
        "endpoint": STEP_PLAN_BASE_URL,
        "model": STEP5_MODEL,
        "billing_surface": "step_plan",
        "reasoning_effort": "medium",
        "python": __import__("sys").version,
        "repeats": args.repeats,
        "concurrency": args.concurrency,
        "fixture_type": "synthetic functional contracts",
        "limitations": [
            "not a semantic retrieval benchmark",
            "unknown external outcomes are not automatically replayed",
        ],
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__).relative_to(Path.cwd()),
                *sorted(Path("plugins/scheduler").glob("*.py")),
                Path("plugins/reply_program/service.py"),
                Path("plugins/ledger/turn_source.py"),
            ]
        },
        "cases": [],
    }
    client = StepPlanClient(api_key=secret, timeout=120)
    limit = asyncio.Semaphore(args.concurrency)

    @asynccontextmanager
    async def rig(work, observed, *, background=False, fail_delivery=False):
        work.mkdir(parents=True, exist_ok=True)
        (work / "config.toml").write_text(
            '[llm.main]\nmodel="step-5-preview"\nbase_url="https://api.stepfun.com/step_plan/v1"\n'
            'reasoning_effort="medium"\n[agent]\nmax_iterations=8\n[memory]\nenabled=false\n'
        )
        runtime = await build_runtime(work)
        runtime.reasoner.model_client = observed
        runtime.reasoner._model_io.model_client = observed
        for name in runtime.tools.names():
            if name not in {
                "read_file",
                "write_file",
                "list_dir",
                "schedule",
                "list_schedules",
                "cancel_schedule",
            }:
                runtime.tools.unregister(name)
        received = []

        async def deliver(message):
            received.append(message)
            if fail_delivery:
                raise RuntimeError("injected failure after possible send")

        runtime.bus.subscribe_outbound("verify", deliver)
        tasks = (
            await runtime.start_background(start_channels=False) if background else []
        )
        dispatcher = (
            None if background else asyncio.create_task(runtime.bus.dispatch_outbound())
        )
        key = "verify:origin"
        session = runtime.session_manager.get_or_create(key)
        if not session.messages:
            session.add_message("user", "Historical release code: HISTORY_" + work.name)
            session.add_message("assistant", "Recorded the release code.")
            runtime.session_manager.save(session)
        runtime.tools.set_context(session_key=key, channel="verify", chat_id="origin")
        try:
            yield SimpleNamespace(
                runtime=runtime,
                scheduler=runtime.scheduler,
                key=key,
                received=received,
                work=work,
                model=observed,
            )
        finally:
            await runtime.stop_background(tasks)
            if dispatcher is not None:
                dispatcher.cancel()
                await asyncio.gather(dispatcher, return_exceptions=True)

    async def create(r, text, *, action="run", context="none"):
        job = json.loads(
            await r.scheduler.schedule(
                text=text, action=action, context=context, trigger="after", when="1h"
            )
        )

        def make_due(state):
            item = state.jobs[job["id"]]
            state.jobs[item.id] = item.model_copy(
                update={"run_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
            )

        r.scheduler.store.change(make_due)
        return job["id"]

    def state(r, job):
        current = r.scheduler.store.read()
        fires = [fire for fire in current.fires.values() if fire.job.id == job]
        return {
            "job_status": current.jobs[job].status,
            "fires": [
                {"status": fire.status, "output_id": fire.output_id} for fire in fires
            ],
            "deliveries": len(r.received),
        }

    async def wait_for(predicate):
        async with asyncio.timeout(120):
            while not predicate():
                await asyncio.sleep(0.05)

    async def experiment(category, index):
        async with limit:
            name = f"{category}_{index}"
            observed = MeteredClient(client)
            started = time.monotonic()
            evidence = {}
            row = {"name": name, "category": category}
            work = args.workspace / name
            nonce = "KIRA_" + uuid4().hex[:12]
            try:
                async with asyncio.timeout(240):
                    if category == "timer_and_nested_notify":
                        async with rig(work, observed, background=True) as r:
                            reply = await r.runtime.process_direct(
                                f'Call schedule exactly once with text="{nonce}", action="notify", trigger="after", when="1s". Then say scheduled.',
                                session_key=r.key,
                                channel="verify",
                                chat_id="origin",
                            )
                            await wait_for(lambda: bool(r.received))
                            await wait_for(
                                lambda: all(
                                    j.status == "completed"
                                    for j in r.scheduler.store.read().jobs.values()
                                )
                            )
                            assert (
                                len(r.received) == 1 and r.received[0].content == nonce
                            )
                            evidence = {
                                "reply": reply.content,
                                "deliveries": len(r.received),
                                "verbatim": True,
                            }
                    elif category == "channel_input_supersession":
                        async with rig(work, observed, background=True) as r:
                            for i in range(3):
                                await r.runtime.bus.publish_inbound(
                                    InboundMessage(
                                        "verify",
                                        "fixture-user",
                                        "origin",
                                        "Cancel and replace all earlier pending requests. "
                                        f"Only write exactly {nonce}_{i} to channel{i}.txt using write_file. "
                                        "Do not execute earlier requested writes. Confirm the code.",
                                    )
                                )
                            await wait_for(lambda: bool(r.received))
                            assert len(r.received) == 1
                            assert (
                                work / "channel2.txt"
                            ).read_text().strip() == f"{nonce}_2"
                            assert not any(
                                (work / f"channel{i}.txt").exists() for i in range(2)
                            )
                            programs = r.runtime.pipeline.programs.store.list(r.key)
                            statuses = [program["status"] for program in programs]
                            assert statuses.count("superseded") == 2
                            assert statuses.count("completed") == 1
                            evidence = {
                                "delivered_turns": 1,
                                "files_verified": 1,
                                "superseded_inputs": 2,
                                "obsolete_effects": 0,
                            }
                    elif category == "parallel_passive_turns":
                        async with rig(work, observed) as r:
                            replies = await asyncio.gather(
                                *[
                                    r.runtime.process_direct(
                                        f"Write exactly {nonce}_{i} to part{i}.txt using write_file. Confirm the code.",
                                        session_key=r.key,
                                        channel="verify",
                                        chat_id="origin",
                                    )
                                    for i in range(3)
                                ]
                            )
                            assert all(
                                (work / f"part{i}.txt").read_text().strip()
                                == f"{nonce}_{i}"
                                for i in range(3)
                            )
                            evidence = {
                                "completed_turns": len(replies),
                                "files_verified": 3,
                            }
                    elif category == "cancel_unknown_tool":
                        async with rig(work, observed) as r:
                            entered = asyncio.Event()
                            effects = []

                            async def blocking_effect():
                                effects.append(nonce)
                                entered.set()
                                await asyncio.Event().wait()

                            r.runtime.tools.register(
                                ToolSpec(
                                    "fixture_effect",
                                    "Record a local fixture effect and wait for cancellation.",
                                    {"type": "object", "properties": {}},
                                ),
                                blocking_effect,
                                risk="external-side-effect",
                            )
                            job = await create(
                                r,
                                "Call fixture_effect exactly once. Do not use any other tools.",
                            )
                            task = asyncio.create_task(r.scheduler.fire(job))
                            try:
                                await asyncio.wait_for(entered.wait(), 120)
                                await r.scheduler.cancel_schedule(job)
                                await asyncio.gather(task, return_exceptions=True)
                            finally:
                                if not task.done():
                                    task.cancel()
                                    await asyncio.gather(task, return_exceptions=True)
                            evidence = state(r, job)
                            assert evidence["fires"][0][
                                "status"
                            ] == "needs_reconciliation" and effects == [nonce]
                            count = len(observed.requests)
                        async with rig(work, observed) as r:
                            await r.scheduler.fire(job)
                            assert len(observed.requests) == count and not r.received
                            evidence.update(effects=len(effects), restart_model_calls=0)
                    else:
                        async with rig(
                            work,
                            observed,
                            fail_delivery=category == "delivery_failure_restart",
                        ) as r:
                            text = f"Use write_file to write exactly {nonce} to result.txt, then read_file to verify it. Answer with the exact code."
                            context = (
                                "session" if category == "session_history" else "none"
                            )
                            if category == "session_history":
                                text = "What is the Historical release code from our previous conversation? Answer with that exact code and nothing else."
                            job = await create(
                                r,
                                text,
                                context=context,
                                action="notify"
                                if category == "notify_receipt"
                                else "run",
                            )
                            if category == "queued_restart":
                                r.scheduler._claim(
                                    job
                                )  # durable queue, before admission
                            elif category == "inflight_restart":
                                fire = r.scheduler._claim(job)
                                r.scheduler._phase(
                                    fire.job.fire_key,
                                    "running",
                                    started_at=datetime.now(timezone.utc),
                                )
                            elif category == "cancel_queued":
                                async with r.runtime.pipeline.admission.hold(r.key):
                                    task = asyncio.create_task(r.scheduler.fire(job))
                                    await asyncio.sleep(0)
                                    await r.scheduler.cancel_schedule(job)
                                    await asyncio.gather(task, return_exceptions=True)
                                assert (
                                    len(observed.requests) == 0
                                    and len(
                                        r.runtime.session_manager.get_or_create(
                                            r.key
                                        ).messages
                                    )
                                    == 2
                                )
                            else:
                                await asyncio.gather(
                                    *[
                                        r.scheduler.fire(job)
                                        for _ in range(
                                            4 if category == "duplicate_restart" else 1
                                        )
                                    ]
                                )
                                evidence = state(r, job)
                                if category == "delivery_failure_restart":
                                    assert (
                                        evidence["fires"][0]["status"]
                                        == "needs_reconciliation"
                                    )
                                else:
                                    assert (
                                        evidence["fires"][0]["status"] == "completed"
                                        and len(r.received) == 1
                                    )
                                    if category == "notify_receipt":
                                        assert (
                                            r.received[0].content == text
                                            and not observed.requests
                                        )
                                    elif category == "session_history":
                                        assert (
                                            "HISTORY_" + work.name
                                            in r.received[0].content
                                        )
                                    else:
                                        assert (
                                            (work / "result.txt").read_text().strip()
                                            == nonce
                                            and nonce in r.received[0].content
                                        )
                                        assert all(
                                            "HISTORY_" + work.name not in prompt
                                            for prompt in observed.inputs
                                        )
                                if category == "memory_source":
                                    messages = r.runtime.session_manager.get_or_create(
                                        r.key
                                    ).messages
                                    pair = messages[-2:]
                                    event = TurnCommitted(
                                        session_key=r.key,
                                        channel="verify",
                                        chat_id="origin",
                                        user_message_id=pair[0]["id"],
                                        assistant_message_id=pair[1]["id"],
                                    )
                                    assert (
                                        load_committed_turn_messages(
                                            work / "sessions.db", event
                                        )
                                        == []
                                    )
                                    human = TurnCommitted(
                                        session_key=r.key,
                                        channel="verify",
                                        chat_id="origin",
                                        user_message_id=messages[0]["id"],
                                        assistant_message_id=messages[1]["id"],
                                    )
                                    assert (
                                        len(
                                            load_committed_turn_messages(
                                                work / "sessions.db", human
                                            )
                                        )
                                        == 2
                                    )
                                    evidence["scheduled_pairs_learned"] = 0
                                    evidence["human_source_messages"] = 2
                            count = len(observed.requests)
                            first_deliveries = len(r.received)
                        async with rig(work, observed) as r:
                            await r.scheduler.fire(job)
                            if category == "queued_restart":
                                assert (
                                    len(observed.requests) > count
                                    and len(r.received) == 1
                                )
                                assert (
                                    work / "result.txt"
                                ).read_text().strip() == nonce
                            else:
                                assert (
                                    len(observed.requests) == count and not r.received
                                )
                            evidence.update(
                                restart=state(r, job),
                                first_deliveries=first_deliveries,
                                restart_model_calls=len(observed.requests) - count,
                            )
                            if category == "inflight_restart":
                                assert (
                                    evidence["restart"]["fires"][0]["status"]
                                    == "needs_reconciliation"
                                )
                    row.update(status="passed", evidence=evidence)
            except Exception as exc:
                row.update(status="failed", error=type(exc).__name__, evidence=evidence)
            row.update(seconds=time.monotonic() - started, requests=observed.requests)
            report["cases"].append(row)
            report["summary"] = summary(report["cases"])
            payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            if secret in payload:
                raise RuntimeError("credential detected in report")
            args.output.write_text(payload)
            print(
                json.dumps(
                    {
                        "name": name,
                        "status": row["status"],
                        "requests": len(observed.requests),
                    }
                ),
                flush=True,
            )

    categories = [
        "tool_write",
        "session_history",
        "timer_and_nested_notify",
        "duplicate_restart",
        "queued_restart",
        "delivery_failure_restart",
        "cancel_unknown_tool",
        "parallel_passive_turns",
        "channel_input_supersession",
        "memory_source",
        "cancel_queued",
        "notify_receipt",
        "inflight_restart",
    ]
    await asyncio.gather(
        *[
            experiment(category, index)
            for index in range(args.repeats)
            for category in categories
        ]
    )
    if report["summary"]["passed"] != report["summary"]["total"]:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if args.repeats < 1 or args.concurrency < 1:
        parser.error("repeats and concurrency must be positive")
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
