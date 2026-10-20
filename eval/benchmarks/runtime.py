"""Offline pressure on the actual pipeline, admission, tools and durable stores.

The deterministic model is a protocol fixture, not a language-model quality test.
One counter effect per ticket must survive current-generation retries exactly once.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path


def distribution(values):
    ordered = sorted(values)
    return {
        f"p{int(q * 100)}": ordered[max(0, math.ceil(len(ordered) * q) - 1)]
        for q in (0.5, 0.95, 0.99)
    }


async def verify(args):
    implementation = args.implementation.resolve()
    sys.path.insert(0, str(implementation))
    from bootstrap.runtime import build_runtime
    from bus.events import InboundMessage
    from bus.events_lifecycle import TurnFinished, TurnStarted
    from core.schema import ModelResponse, ToolCall, ToolSpec

    if (implementation / "plugins/models/types.py").is_file():
        from plugins.models.types import RetryableModelError
    else:
        from agent.model_runtime.types import RetryableModelError

    args.workspace.mkdir(parents=True, exist_ok=False)
    if args.output.exists():
        raise ValueError("choose a fresh output")
    for name in (
        "LIGHT_MODEL_ID",
        "LIGHT_OPENAI_COMPATIBLE_BASE_URL",
        "LIGHT_OPENAI_COMPATIBLE_API_KEY",
    ):
        os.environ.pop(name, None)
    os.environ.update(
        MODEL_ID="offline-fixture",
        OPENAI_COMPATIBLE_BASE_URL="http://offline.invalid/v1",
        OPENAI_COMPATIBLE_API_KEY="offline-fixture",
        AGENT_STREAM="false",
    )
    (args.workspace / "config.toml").write_text(
        '[llm.main]\nmodel="offline-fixture"\nbase_url="http://offline.invalid/v1"\n[memory]\nenabled=false\n'
    )
    if args.without_extensions:
        directory = args.workspace / ".kirakira"
        directory.mkdir()
        (directory / "manifest.toml").write_text(
            "[plugins.curated_feeds]\nenabled=false\n"
            '[plugins."curated-feeds"]\nenabled=false\n'
        )
    effects, injected, requests = Counter(), set(), 0
    request_digests = {}
    read_active, peak_read = Counter(), 0
    turn_active, peak_turn = Counter(), 0
    latencies, queue_wait, active_time, loop_lag, rows = [], [], [], [], []

    class Model:
        async def acomplete(self, messages, *args, **kwargs):
            nonlocal requests
            requests += 1
            await asyncio.sleep(0)
            current = next(
                m
                for m in reversed(messages)
                if m["role"] == "user" and "TICKET:" in str(m.get("content"))
            )
            ticket = int(re.search(r"TICKET:(\d+)", str(current["content"]))[1])
            wire = json.dumps(
                {"messages": messages, "tools": [asdict(spec) for spec in args[0]]},
                sort_keys=True,
                ensure_ascii=False,
            ).replace(str(args_workspace), "<fixture-workspace>")
            request_digests.setdefault(ticket, []).append(
                hashlib.sha256(wire.encode()).hexdigest()
            )
            if messages[-1]["role"] == "tool":
                if ticket % 13 == 0 and ticket not in injected:
                    injected.add(ticket)
                    raise RetryableModelError(
                        "fixture: recover this generation", retry_after=0
                    )
                return ModelResponse(text=f"done TICKET:{ticket}")
            return ModelResponse(
                tool_calls=[
                    *[
                        ToolCall(
                            f"{ticket}:read:{i}",
                            "read_value",
                            {"ticket": ticket, "index": i},
                        )
                        for i in range(4)
                    ],
                    ToolCall(f"{ticket}:record", "record", {"ticket": ticket}),
                ]
            )

    args_workspace = args.workspace
    runtime = await build_runtime(args.workspace)
    runtime.reasoner._model_io.model_client = Model()
    runtime.reasoner.config.history_window = 100_000
    for name in runtime.tools.names():
        runtime.tools.unregister(name)

    async def read(ticket, index):
        nonlocal peak_read
        read_active[ticket] += 1
        peak_read = max(peak_read, read_active[ticket])
        try:
            await asyncio.sleep(0.001)
            return str(ticket * 4 + index)
        finally:
            read_active[ticket] -= 1

    async def record(ticket):
        assert read_active[ticket] == 0, "write crossed an unfinished read barrier"
        effects[ticket] += 1
        return f"recorded {ticket}"

    schema = {
        "type": "object",
        "properties": {"ticket": {"type": "integer"}, "index": {"type": "integer"}},
    }
    runtime.tools.register(
        ToolSpec("read_value", "independent read", schema), read, parallel_safe=True
    )
    runtime.tools.register(
        ToolSpec("record", "counter effect", schema), record, risk="write"
    )

    def started(event):
        nonlocal peak_turn
        turn_active[event.session_key] += 1
        peak_turn = max(peak_turn, turn_active[event.session_key])
        assert turn_active[event.session_key] == 1

    runtime.event_bus.on(TurnStarted, started)
    runtime.event_bus.on(
        TurnFinished, lambda event: turn_active.subtract([event.session_key])
    )
    monitoring = True

    async def monitor():
        while monitoring:
            before = time.perf_counter()
            await asyncio.sleep(0.005)
            loop_lag.append(max(0, (time.perf_counter() - before - 0.005) * 1000))

    async def turn(ticket):
        queued = time.perf_counter()
        admitted = None

        def on_admit():
            nonlocal admitted
            admitted = time.perf_counter()

        key = f"stress:{ticket % args.sessions}"
        message = InboundMessage(
            "stress",
            "fixture",
            str(ticket % args.sessions),
            f"TICKET:{ticket}",
            timestamp=datetime(2026, 10, 19, tzinfo=UTC),
            metadata={
                "source": "programmatic",
                "skip_memory_retrieval": True,
                "skip_post_memory": True,
            },
        )
        try:
            result = await runtime.pipeline.run(
                message, key, dispatch_outbound=False, admitted=on_admit
            )
            assert result.content == f"done TICKET:{ticket}", result.content
            assert effects[ticket] == 1
            rows.append({"ticket": ticket, "status": "passed"})
        except Exception as exc:
            rows.append(
                {
                    "ticket": ticket,
                    "status": "failed",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:500],
                }
            )
        finally:
            finished = time.perf_counter()
            latencies.append((finished - queued) * 1000)
            if admitted is not None:
                queue_wait.append((admitted - queued) * 1000)
                active_time.append((finished - admitted) * 1000)

    monitoring_task = asyncio.create_task(monitor())
    before = time.perf_counter()
    try:
        await asyncio.gather(*(turn(ticket) for ticket in range(args.turns)))
        elapsed = time.perf_counter() - before
        runs = runtime.reasoner.execution_store.recent(args.turns + 1)
        statuses = Counter(row["status"] for row in runs)
        unresolved = sum(
            bool(runtime.reasoner.execution_store.unresolved(row["run_id"]))
            for row in runs
        )
        expected = Counter({ticket: 1 for ticket in range(args.turns)})
        assert effects == expected and unresolved == 0
        assert statuses == {"completed": args.turns}
        assert peak_turn == 1 and peak_read == 4
        assert sum(turn_active.values()) == 0
        assert all(
            not runtime.pipeline.admission.is_busy(f"stress:{i}")
            for i in range(args.sessions)
        )
    finally:
        monitoring = False
        await monitoring_task
        await runtime.stop_background([])

    restarted = await build_runtime(args.workspace)
    try:
        recovered_statuses = Counter(
            row["status"]
            for row in restarted.reasoner.execution_store.recent(args.turns + 1)
        )
        persisted_messages = sum(
            len(restarted.session_manager.get_or_create(f"stress:{i}").messages)
            for i in range(args.sessions)
        )
        assert recovered_statuses == statuses and persisted_messages == args.turns * 2
    finally:
        await restarted.stop_background([])
    report = {
        "fixture": "offline production pipeline pressure",
        "implementation": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=implementation, text=True
        ).strip(),
        "implementation_worktree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=implementation, text=True
            ).strip()
        ),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "without_extensions": args.without_extensions,
        "python": platform.python_version(),
        "model_network_calls": 0,
        "turns": args.turns,
        "sessions": args.sessions,
        "passed": sum(row["status"] == "passed" for row in rows),
        "elapsed_seconds": elapsed,
        "turns_per_second": args.turns / elapsed,
        "latency_ms": distribution(latencies),
        "queue_wait_ms": distribution(queue_wait),
        "admitted_to_finish_ms": distribution(active_time),
        "event_loop_lag_ms": distribution(loop_lag),
        "fixture_model_attempts": requests,
        "injected_retries": len(injected),
        "tool_calls": args.turns * 5,
        "effects": sum(effects.values()),
        "peak_turns_per_session": peak_turn,
        "peak_parallel_reads_per_turn": peak_read,
        "unresolved_runs": unresolved,
        "restart_messages": persisted_messages,
        "execution_statuses": dict(statuses),
        "request_digests": request_digests,
        "cases": sorted(rows, key=lambda row: row["ticket"]),
        "source_sha256": {
            str(path.relative_to(implementation)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in [
                *[
                    path
                    for owner in (
                        "agent/core",
                        "agent/composition",
                        "agent/plugin_composition",
                        "bootstrap",
                        "plugins/conversation",
                        "plugins/reply_program",
                        "plugins/sources",
                        "plugins/ledger",
                        "plugins/turn_projection",
                        "plugins/jobs",
                        "plugins/delivery",
                        "plugins/reply",
                        "plugins/subagent",
                        "plugins/react",
                        "plugins/models",
                        "plugins/execution",
                        "plugins/tools",
                    )
                    for path in sorted((implementation / owner).glob("*.py"))
                ],
                implementation / "agent/tools/registry.py",
                implementation / "utils/async_tasks.py",
            ]
            if path.exists()
        },
        "limitations": [
            "deterministic model, not real task success or provider throughput",
            "includes SQLite durability and concurrent event-loop load on this machine",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"cases", "source_sha256"}
            },
            indent=2,
        )
    )
    if report["passed"] != args.turns:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--turns", type=int, default=1000)
    parser.add_argument("--sessions", type=int, default=64)
    parser.add_argument("--implementation", type=Path, default=Path.cwd())
    parser.add_argument("--without-extensions", action="store_true")
    args = parser.parse_args()
    if args.turns < 1 or not 1 <= args.sessions <= args.turns:
        parser.error("require 1 <= sessions <= turns")
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
