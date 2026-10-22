"""Observe provider cache usage without changing production prompts or routing.

The synthetic ordering probe is a mechanism experiment, not a product speedup.
Mail mode traces the existing native owner-correction acceptance unchanged.
Only request hashes and sizes are saved, never credentials or prompt bodies.
"""

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from eval.models import EvaluationModel, provider_slot, source_identity
from kirakira_agent.runtime import ROOT, initialize_workspace, runtime
from plugins.models.contract import CHAT_MODELS, ModelRequest


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Trace:
    def __init__(self):
        self.rows = []
        self.label = "native-mail"
        self.previous = defaultdict(list)
        self.active = ContextVar("cache_probe_request", default=None)
        self.installed = set()
        self.expected_fragments = ()

    def install_loaded(self, stack):
        # Native plugins import isolated generation modules. Patching the public
        # package alone would observe no calls made by those actual owners.
        for module in tuple(sys.modules.values()):
            cls = getattr(module, "_BoundChat", None)
            if isinstance(cls, type) and hasattr(cls, "_encode_body") and cls not in self.installed:
                self.install(stack, module)
                self.installed.add(cls)

    def install(self, stack, driver):
        original_complete = driver._BoundChat.complete
        original_encode = driver._BoundChat._encode_body

        def encode(bound, body, request):
            encoded = original_encode(bound, body, request)
            row = self.active.get()
            if row is not None:
                wire = json.loads(encoded)
                messages = wire.pop("messages")
                # Compare like settings/tools, independent of request-only keys.
                settings = {k: v for k, v in wire.items() if k not in {"stream", "stream_options", "prompt_cache_key"}}
                key = digest(settings)
                serialized = [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in messages]
                best, best_chars = 0, 0
                for previous in self.previous[key][-128:]:
                    shared, chars = 0, 0
                    for before, after in zip(previous, serialized):
                        if before != after:
                            for a, b in zip(before, after):
                                if a != b:
                                    break
                                chars += 1
                            break
                        shared += 1
                        chars += len(after)
                    best = max(best, shared)
                    best_chars = max(best_chars, chars)
                self.previous[key].append(serialized)
                row.update(
                    settings_sha256=key, tools_sha256=digest(wire.get("tools", [])),
                    system_sha256=digest([m for m in messages if m["role"] == "system"]),
                    message_sha256=[digest(m) for m in messages],
                    message_roles=[m["role"] for m in messages],
                    message_chars=[len(m) for m in serialized],
                    common_complete_messages=best,
                    common_complete_message_chars=sum(len(m) for m in serialized[:best]),
                    common_message_prefix_chars=best_chars,
                    prompt_cache_key_sent="prompt_cache_key" in wire,
                )
                if self.expected_fragments:
                    system = "\n".join(str(m.get("content", "")) for m in messages if m["role"] == "system")
                    row["expected_system_fragments"] = {fragment: fragment in system for fragment in self.expected_fragments}
            return encoded

        async def complete(bound, request):
            started = time.monotonic()
            row = {"group": self.label, "ordinal": len(self.rows), "first_delta_ms": None}
            self.rows.append(row)
            token = self.active.set(row)
            original_delta = request.on_delta

            async def delta(value):
                if row["first_delta_ms"] is None and any(value.values()):
                    row["first_delta_ms"] = round((time.monotonic() - started) * 1000, 3)
                await original_delta(value)

            try:
                # Preserve the caller's streaming/non-streaming choice.
                response = await original_complete(bound, replace(request, on_delta=delta) if original_delta else request)
                row.update(state="success", usage=None if response.usage is None else asdict(response.usage))
                return response
            except Exception as error:
                row.update(state="error", error_type=type(error).__name__)
                raise
            finally:
                row["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                self.active.reset(token)

        stack.enter_context(patch.object(driver._BoundChat, "complete", complete))
        stack.enter_context(patch.object(driver._BoundChat, "_encode_body", encode))


def summary(rows):
    groups = {}
    for name in sorted({r["group"] for r in rows}):
        selected = [r for r in rows if r["group"] == name]
        usages = [r.get("usage") or {} for r in selected]
        observed = [u for u in usages if type(u.get("cached_input_tokens")) is int and type(u.get("input_tokens")) is int]
        prompt = sum(u["input_tokens"] for u in observed)
        hit = sum(u["cached_input_tokens"] for u in observed)
        first = [r["first_delta_ms"] for r in selected if r["first_delta_ms"] is not None]
        groups[name] = {
            "calls": len(selected), "successes": sum(r.get("state") == "success" for r in selected),
            "cache_observed_calls": len(observed), "cache_observed_input_tokens": prompt,
            "cached_input_tokens": hit, "cached_input_ratio": hit / prompt if prompt else None,
            "output_tokens": sum(u.get("output_tokens") or 0 for u in usages),
            "median_first_delta_ms": statistics.median(first) if first else None,
            "median_duration_ms": statistics.median(r["duration_ms"] for r in selected),
            "distinct_system_prompts": len({r.get("system_sha256") for r in selected}),
            "correct": sum(r.get("correct") is True for r in selected),
            "scored": sum("correct" in r for r in selected),
        }
    return groups


async def probe(args, model, trace, stack):
    initialize_workspace(args.workspace)
    nonce = uuid4().hex
    # Identical information in both arms; only the volatile marker moves.
    stable = "\n".join(f"资料{i:03}: 青禾项目归档编号{i:03}；旧记录仅作背景，预算以本次用户规则为准。" for i in range(160))
    stable = f"实验档案 {nonce}\n" + stable
    async with runtime(args.workspace) as core:
        trace.install_loaded(stack)
        trace.label = "setup"
        await model.configure(core, args.key_file)
        context = core.plugin_manager.live_root.context
        async with context.require(CHAT_MODELS).independent_execution(
            model_id=model.model_id, reasoning_effort=model.reasoning_effort,
        ) as execution:
            for round_index in range(args.rounds):
                order = ("stable-first", "volatile-first") if round_index % 2 == 0 else ("volatile-first", "stable-first")
                for layout in order:
                    trace.label = layout
                    volatile = "当前请求标记 " + uuid4().hex
                    system = "\n\n".join((stable, volatile) if layout == "stable-first" else (volatile, stable))
                    budget = 90 if round_index % 2 == 0 else 130
                    expected = "skip" if budget < 110 else "share"
                    user = f"当前用户规则：总价上限{budget}元。新到货总价110元，免运费。预算内提醒，超出跳过。只输出JSON，格式为{{\"decision\":\"share或skip\"}}。"

                    async def on_delta(value):
                        pass

                    response = await execution.chat("default").complete(ModelRequest(
                        messages=({"role": "system", "content": system}, {"role": "user", "content": user}),
                        max_output_tokens=512, on_delta=on_delta,
                    ))
                    try:
                        value = (response.content or "").strip().removeprefix("```json").removesuffix("```").strip()
                        decision = json.loads(value)["decision"]
                    except (ValueError, KeyError, TypeError):
                        decision = "invalid"
                    trace.rows[-1].update(round=round_index, budget=budget, expected=expected, decision=decision,
                                          correct=decision == expected, finish_reason=response.finish_reason)
                    print(json.dumps({"layout": layout, "round": round_index, "correct": decision == expected}), flush=True)


async def evaluate(args):
    model = EvaluationModel.read(args.model_config)
    if model.driver_id not in {"openai-compatible", "step-plan"}:
        raise ValueError("wire tracing currently supports openai-compatible and step-plan only")
    if args.workspace.exists():
        raise ValueError("use a fresh local workspace to preserve run boundaries")
    trace = Trace()
    implementation = source_identity()
    for name in ("eval/prompt_cache.py", "plugins/models/usage.py", "plugins/openai_compatible/driver.py",
                 "plugins/context/plugin.py", "plugins/prompt/plugin.py", "plugins/markdown_memory/plugin.py",
                 "frontend/plugins/models/src/cache-usage.js"):
        implementation[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    report = {"implementation": implementation, "model_config": model.description(), "mode": args.mode,
              "scope": "single-provider mechanism/functional observation; not a product A/B speedup or benchmark",
              "prefix_measure": "complete JSON messages with equal tools/settings; character counts are not provider KV tokens",
              "rounds": args.rounds if args.mode == "probe" else None}
    try:
        with ExitStack() as stack:
            async with provider_slot(model, args.key_file):
                if args.mode == "probe":
                    await probe(args, model, trace, stack)
                    report["passed"] = all(r["correct"] for r in trace.rows if "correct" in r)
                else:
                    from eval import mail_followups

                    @asynccontextmanager
                    async def traced_runtime(*values, **options):
                        async with runtime(*values, **options) as core:
                            trace.install_loaded(stack)
                            yield core

                    stack.enter_context(patch.object(mail_followups, "runtime", traced_runtime))

                    functional = args.output.with_suffix(".functional.json")
                    mail_args = SimpleNamespace(**{**vars(args), "output": functional}, functional_only=True,
                                                with_functional=True, family_start=0, family_end=0)
                    report["passed"] = await mail_followups.evaluate(mail_args)
                    report["functional_report"] = functional.name
                    from plugins.models.contract import MODEL_CACHE_USAGE

                    async with runtime(args.workspace) as core:
                        context = core.plugin_manager.live_root.context
                        for _ in range(60):
                            overview = await context.require(MODEL_CACHE_USAGE).overview()
                            if overview["complete"]:
                                break
                            await asyncio.sleep(0.05)
                        if not overview["complete"]:
                            raise TimeoutError("native usage projection did not finish scanning")
                        report["native_cache_overview"] = overview
    finally:
        report.update(calls=trace.rows, summary=summary(trace.rows))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report.get("passed", False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "key-file", "model-config", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--mode", choices=("probe", "mail"), default="probe")
    parser.add_argument("--embedding-manifest", type=Path)
    parser.add_argument("--attention-cases", action="store_true")
    parser.add_argument("--rounds", type=int, default=8)
    args = parser.parse_args()
    if args.rounds < 2 or (args.mode == "mail" and args.embedding_manifest is None):
        parser.error("at least two rounds; mail mode requires --embedding-manifest")
    os.environ["AKASHIC_PLUGIN_HOME"] = str(args.workspace.parent / (args.workspace.name + "-home"))
    raise SystemExit(0 if asyncio.run(evaluate(args)) else 1)


if __name__ == "__main__":
    main()
