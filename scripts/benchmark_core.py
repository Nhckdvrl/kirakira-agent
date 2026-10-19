"""Matched, offline core workloads; use --implementation for an older checkout.

Only temporary SQLite/files are written. Model latency is deliberately excluded.
Raw samples, correctness checks and implementation identity accompany timings.
"""

import argparse
import asyncio
import hashlib
import inspect
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def summary(samples):
    ordered = sorted(samples)
    return {
        "samples_ms": samples,
        "p50_ms": statistics.median(samples),
        "p95_ms": ordered[max(0, int(len(ordered) * 0.95 + 0.999) - 1)],
    }


async def measure(a):
    sys.path.insert(0, str(a.implementation.resolve()))
    from agent.core.contracts import RuntimeConfig
    from agent.core.execution import ExecutionStore
    from agent.core.model_io import ModelInvoker
    from agent.core.reasoner import DefaultReasoner
    from agent.tools.registry import ToolRegistry
    from bus.event_bus import EventBus
    from bus.events_lifecycle import StreamDeltaReady
    from core.schema import ModelResponse, ToolCall, ToolSpec

    report = {
        "implementation": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=a.implementation, text=True
        ).strip(),
        "implementation_sources": {
            str(path.relative_to(a.implementation)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in [
                *sorted((a.implementation / "agent/core").glob("*.py")),
                a.implementation / "agent/tools/registry.py",
                a.implementation / "utils/async_tasks.py",
            ]
            if path.exists()
        },
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "clock": "perf_counter_ns",
        "network": False,
        "repeats": a.repeats,
        "warmups": 2,
        "workloads": {},
    }
    for chunks in (0, 4, 2000):
        deltas = []

        class Stream:
            def complete_stream(self, *args, **kwargs):
                raise AssertionError("async stream required")

            async def acomplete_stream(self, *args, **kwargs):
                for i in range(chunks):
                    args[5](str(i) + ",", "")
                # Completion happens after the last callback, as in an HTTP stream.
                await asyncio.sleep(0.001)
                return ModelResponse(text="done")

        bus = EventBus()
        bus.on(StreamDeltaReady, lambda event: deltas.append(event.content_delta))
        invoker = ModelInvoker(Stream(), RuntimeConfig(model="fixture"), bus)
        samples = []
        for repetition in range(a.repeats + 2):
            deltas.clear()
            started = time.perf_counter_ns()
            result = await invoker.complete(
                [], [], session_key="bench", channel="test", chat_id="1", iteration=0
            )
            elapsed = (time.perf_counter_ns() - started) / 1e6
            assert result.text == "done"
            assert deltas == [str(i) + "," for i in range(chunks)]
            if repetition >= 2:
                samples.append(elapsed)
        report["workloads"][f"stream_{chunks}_chunks"] = summary(samples)

    with tempfile.TemporaryDirectory(prefix="kirakira-core-bench-") as temporary:
        root = Path(temporary)
        for count in (10, 50, 100):
            store = ExecutionStore(root / f"journal-{count}.db")
            try:
                run_id = store.start(
                    session_key="bench", scope="test", parent_id=None, inputs={}
                )
                for i in range(count):
                    store.append(run_id, "model_request", {"messages": "x" * 32768})
                    store.append(run_id, "model_response", {"text": "y" * 1024})
                    store.append(run_id, "tool_intent", {"operation_id": str(i)})
                    if i != count - 1:
                        store.append(run_id, "tool_result", {"operation_id": str(i)})
                samples = []
                for repetition in range(a.repeats + 2):
                    started = time.perf_counter_ns()
                    pending = store.unresolved(run_id)
                    elapsed = (time.perf_counter_ns() - started) / 1e6
                    assert pending == [{"operation_id": str(count - 1)}]
                    if repetition >= 2:
                        samples.append(elapsed)
                report["workloads"][f"journal_{count}_steps"] = summary(samples)
            finally:
                store.close()

        registry = ToolRegistry()
        active, peak = 0, 0

        async def lookup(index):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.025)
                return str(index * 7)
            finally:
                active -= 1

        supports_parallel = (
            "parallel_safe" in inspect.signature(registry.register).parameters
        )
        registry.register(
            ToolSpec("lookup", "independent read", {"type": "object"}),
            lookup,
            **({"parallel_safe": True} if supports_parallel else {}),
        )

        class Model:
            async def acomplete(self, messages, *args, **kwargs):
                if messages[-1]["role"] == "user":
                    return ModelResponse(
                        tool_calls=[
                            ToolCall(str(i), "lookup", {"index": i}) for i in range(8)
                        ]
                    )
                assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
                    str(i) for i in range(8)
                ]
                return ModelResponse(text="done")

        store = ExecutionStore(root / "fanout.db")
        try:
            engine = DefaultReasoner(
                model_client=Model(),
                tools=registry,
                config=RuntimeConfig(model="fixture", stream=False),
                context=None,
                event_bus=EventBus(),
                execution_store=store,
            )
            samples = []
            for repetition in range(a.repeats + 2):
                started = time.perf_counter_ns()
                result = await engine.run(
                    [{"role": "user", "content": "read eight independent values"}],
                    session_key="bench",
                    channel="test",
                    chat_id="1",
                    request_text="read",
                )
                elapsed = (time.perf_counter_ns() - started) / 1e6
                assert (
                    result.reply == "done" and len(result.tool_chain[0]["calls"]) == 8
                )
                assert [c["result"] for c in result.tool_chain[0]["calls"]] == [
                    str(i * 7) for i in range(8)
                ]
                assert store.unresolved(result.execution_id) == [] and active == 0
                if repetition >= 2:
                    samples.append(elapsed)
            report["workloads"]["eight_independent_tools"] = {
                **summary(samples),
                "peak_active": peak,
                "explicit_parallel_opt_in": supports_parallel,
                "tool_delay_ms": 25,
                "ordered_results": True,
            }
        finally:
            store.close()
            await registry.shutdown()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation", type=Path, default=Path.cwd())
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("--repeats must be at least two")
    report = asyncio.run(measure(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: {key: value for key, value in row.items() if key != "samples_ms"}
                for k, row in report["workloads"].items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
