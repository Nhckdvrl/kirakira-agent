"""Real Step5 memory tool chains on isolated synthetic histories.

Chat uses Token Plan exclusively. Akasha embeddings are local deterministic
vectors: these cases verify tool/source/restart integration, not recall quality.
"""

import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from core.memory.legacy import MemoryRuntime
from core.net.http import SharedHttpResources
from eval.longmemeval.offline_ab import DeterministicEmbedder
from infra.providers.step_plan import STEP5_MODEL, STEP_PLAN_BASE_URL, StepPlanClient
from plugins.akasha.config import AkashaConfig
from plugins.akasha.engine import AkashaMemoryEngine
from plugins.akasha2.engine import Akasha2MemoryEngine
from plugins.default_memory.config_models import (
    Config,
    MemoryConfig,
    MemoryEmbeddingConfig,
)
from plugins.default_memory.contract import MemoryServices
from plugins.ledger.manager import SessionManager
from plugins.memory_tools.tools import register_memory_tools
from plugins.react.policy import ReasoningPolicy
from plugins.reply_program.scoped import run_scoped
from plugins.tools.registry import ToolRegistry


async def make_engine(kind, path):
    embedder = DeterministicEmbedder()
    config = Config(
        model="unused",
        memory=MemoryConfig(
            embedding=MemoryEmbeddingConfig(
                model=embedder.model_id,
                base_url="offline://no-network",
                output_dimensionality=64,
            )
        ),
    )
    if kind == "akasha2":
        return Akasha2MemoryEngine(
            config=config,
            workspace=path,
            http_resources=SharedHttpResources(),
            embedder=embedder,
        )
    engine = AkashaMemoryEngine(
        config=config,
        akasha_config=AkashaConfig(),
        workspace=path,
        http_resources=SharedHttpResources(),
    )
    await engine._embedder.aclose()
    engine._embedder = embedder
    return engine


async def verify(a):
    a.workspace.mkdir(parents=True, exist_ok=False)
    client = StepPlanClient(
        api_key=a.key_file.read_text().strip(), reasoning_effort="medium", timeout=120
    )
    report = {
        "endpoint": STEP_PLAN_BASE_URL,
        "model": STEP5_MODEL,
        "reasoning_effort": "medium",
        "billing_surface": "step_plan",
        "embedding": "offline-hash-v1 (integration only)",
        "cases": [],
    }

    async def record(name, action):
        start = time.monotonic()
        try:
            evidence = await action()
            case = {"name": name, "status": "passed", "evidence": evidence}
        except Exception as exc:
            case = {
                "name": name,
                "status": "failed",
                "error": type(exc).__name__,
                "detail": str(exc),
            }
        case["seconds"] = time.monotonic() - start
        report["cases"].append(case)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"name": name, "status": case["status"]}), flush=True)

    async def run(tools, prompt):
        result = await run_scoped(
            model_client=client,
            registry=tools,
            model=STEP5_MODEL,
            system="Follow the requested tool procedure. Never invent memory evidence. Use only the available memory tools; no external actions.",
            prompt=prompt,
            session_key="verification:memory-tools",
            max_steps=8,
            max_tokens=8192,
            policy=ReasoningPolicy(allowed_tools=frozenset(tools.names())),
        )
        return result

    # Explicit writes are supported only by the legacy fallback in this fixture.
    path = a.workspace / "lexical"
    memory = MemoryRuntime(path, engine="legacy")
    tools = ToolRegistry()
    register_memory_tools(tools, memory=memory)
    nonce = "KIRA_" + uuid4().hex[:12]
    try:

        async def remember():
            result = await run(
                tools,
                f'Call memorize to remember this exact preference: "For the verification fixture, my preferred deployment codename is {nonce}." Confirm afterward.',
            )
            assert "memorize" in result.tools_used
            assert any(
                nonce in r.content for r in memory._records if r.status == "active"
            )
            return {"tools": result.tools_used, "reply": result.reply}

        await record("lexical_memorize", remember)

        async def forget():
            result = await run(
                tools,
                'First recall_memory with query "deployment codename". Then forget_memory using every returned matching item id. Confirm deletion. Do not create a memory.',
            )
            assert {"recall_memory", "forget_memory"} <= set(result.tools_used)
            assert not any(
                nonce in r.content for r in memory._records if r.status == "active"
            )
            return {"tools": result.tools_used, "reply": result.reply}

        await record("lexical_recall_and_forget", forget)
    finally:
        await tools.shutdown()
        await memory.shutdown()

    for kind in ("akasha", "akasha2"):
        path = a.workspace / kind
        path.mkdir()
        sessions = SessionManager(path)
        engine = await make_engine(kind, path)
        await engine.catch_up()  # establish cutover before the new publications
        nonce = "FINAL_" + uuid4().hex[:12]
        at = datetime.now(timezone.utc) - timedelta(hours=1)
        for index, text in enumerate(
            (
                "For deployment-handoff the old release code was OLD_OBSOLETE.",
                "Unrelated cafe note: I prefer tea.",
                f"Correction for deployment-handoff: the final release code is {nonce}. OLD_OBSOLETE is invalid. Use the final code.",
            )
        ):
            session = sessions.get_or_create("fixture:" + str(index))
            stamp = (at + timedelta(minutes=index)).isoformat()
            session.add_message("user", text, timestamp=stamp)
            session.add_message(
                "assistant",
                "Recorded the fixture information: " + text,
                timestamp=stamp,
            )
            sessions.save(session)
        await engine.catch_up()
        try:
            for phase in ("live", "restart"):
                if phase == "restart":
                    await MemoryServices(engine=engine).aclose()
                    engine = await make_engine(kind, path)
                    await engine.catch_up()
                tools = ToolRegistry()
                register_memory_tools(
                    tools,
                    session_manager=sessions,
                    memory_services=MemoryServices(engine=engine),
                    read_only=True,
                )

                observed_fetches = []
                fetch = tools.get_tool("fetch_messages")
                original_fetch = fetch.handler

                def capture_fetch(source_ref, context=2):
                    result = original_fetch(source_ref, context)
                    observed_fetches.append(
                        {
                            "source_ref": source_ref,
                            "returned_ids": [
                                row["source_ref"] for row in json.loads(result)
                            ],
                        }
                    )
                    return result

                fetch.handler = capture_fetch

                async def recall(tools=tools):
                    result = await run(
                        tools,
                        'What is the final deployment-handoff release code? First recall_memory with query "deployment-handoff release code". Then fetch_messages with a returned source_ref to check the original correction. Answer only the final code, not the obsolete code. Do not write memory.'
                        + (
                            " Pass a returned source_ref unchanged to fetch_messages, including the complete JSON ID list; do not split it into single IDs."
                            if kind == "akasha2"
                            else ""
                        ),
                    )
                    diagnostic = json.dumps(
                        {
                            "tools": result.tools_used,
                            "reply": result.reply,
                            "fetches": observed_fetches,
                        },
                        ensure_ascii=False,
                    )
                    assert {"recall_memory", "fetch_messages"} <= set(
                        result.tools_used
                    ), diagnostic
                    assert (
                        nonce in result.reply and "OLD_OBSOLETE" not in result.reply
                    ), diagnostic
                    if kind == "akasha2":
                        ranges = [
                            entry
                            for entry in observed_fetches
                            if entry["source_ref"].startswith("[")
                        ]
                        assert ranges, (
                            "model did not exercise the unchanged recall range: "
                            + diagnostic
                        )
                        assert all(
                            set(json.loads(entry["source_ref"]))
                            <= set(entry["returned_ids"])
                            for entry in ranges
                        ), diagnostic
                    assert set(tools.names()) == {
                        "recall_memory",
                        "search_messages",
                        "fetch_messages",
                    }
                    return {
                        "tools": result.tools_used,
                        "reply": result.reply,
                        "fetches": observed_fetches,
                        "messages": sum(
                            len(s.messages) for s in sessions._cache.values()
                        ),
                    }

                try:
                    await record(kind + "_recall_fetch_" + phase, recall)
                finally:
                    await tools.shutdown()
        finally:
            await MemoryServices(engine=engine).aclose()
            sessions.close()
    report["passed"] = sum(c["status"] == "passed" for c in report["cases"])
    report["total"] = len(report["cases"])
    a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if report["passed"] != report["total"]:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(verify(parser.parse_args()))


if __name__ == "__main__":
    main()
