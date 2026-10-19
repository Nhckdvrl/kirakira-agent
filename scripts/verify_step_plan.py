"""Exercise real Kirakira core and Wake with Step5 on Token Plan only.

All workspace writes happen under --workspace. Keys are read from --key-file,
passed in memory, and never copied into config, reports, or model prompts.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from agent.core.contracts import RuntimeConfig
from agent.core.execution import ExecutionStore
from agent.core.reasoner import DefaultReasoner
from agent.model_runtime.types import RetryableModelError
from agent.tools.registry import ToolRegistry
from agent.turns.policy import ReasoningPolicy
from bootstrap.app import build_runtime
from bus.event_bus import EventBus
from core.schema import ToolSpec
from infra.providers.step_plan import StepPlanClient, STEP5_MODEL, STEP_PLAN_BASE_URL
from plugins.proactive_flow.judge import ProactiveJudge
from proactive_v2.contracts import ContentContract


class ObservedClient:
    def __init__(self, client, *, inject_failure=False):
        self.client = client
        self.requests = []
        self.inject_failure = inject_failure
        self.injected = 0

    async def acomplete(self, *args, **kwargs):
        if self.inject_failure and len(self.requests) == 1 and self.injected == 0:
            self.injected += 1
            raise RetryableModelError(
                "verification: injected interruption after completed tool",
                retry_after=0,
            )
        response = await self.client.acomplete(*args, **kwargs)
        self.requests.append(
            {"usage": response.usage, "stop_reason": response.stop_reason}
        )
        return response


async def verify(a):
    a.workspace.mkdir(parents=True, exist_ok=True)
    report = {
        "endpoint": STEP_PLAN_BASE_URL,
        "model": STEP5_MODEL,
        "billing_surface": "step_plan",
        "reasoning_effort": "medium",
        "cases": [],
    }
    client = StepPlanClient(
        api_key=a.key_file.read_text().strip(), thinking_enabled=False, timeout=120
    )

    async def record(name, action):
        started = time.monotonic()
        try:
            evidence = await action()
            row = {"name": name, "status": "passed", "evidence": evidence}
        except Exception as exc:
            row = {"name": name, "status": "failed", "error": type(exc).__name__}
        row["seconds"] = time.monotonic() - started
        report["cases"].append(row)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"name": name, "status": row["status"]}), flush=True)

    for index in range(6):

        async def tool_case(i=index):
            registry = ToolRegistry()
            writes = []
            nonce = uuid4().hex[:12]

            def lookup(code):
                if code != nonce:
                    raise ValueError("unknown code")
                return {"code": code, "value": 100 + i}

            def save(value):
                if value != (100 + i) * 3:
                    raise ValueError("incorrect calculation")
                writes.append(value)
                return "saved successfully"

            registry.register(
                ToolSpec(
                    "lookup",
                    "Read the value for the exact code",
                    {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                        "required": ["code"],
                    },
                ),
                lookup,
            )
            registry.register(
                ToolSpec(
                    "save",
                    "Persist the calculated integer exactly once",
                    {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                    },
                ),
                save,
            )
            observed = ObservedClient(client, inject_failure=i % 2 == 0)
            store = ExecutionStore(a.workspace / f"tool-{i}.db")
            reasoner = DefaultReasoner(
                model_client=observed,
                tools=registry,
                config=RuntimeConfig(model=STEP5_MODEL, stream=False, max_tokens=2048),
                context=None,
                event_bus=EventBus(),
                execution_store=store,
            )
            try:
                result = await reasoner.run(
                    [
                        {
                            "role": "user",
                            "content": f"Look up code {nonce}, multiply its value by three, then save the result exactly once. Report the result.",
                        }
                    ],
                    session_key=f"verify:{i}",
                    channel="verify",
                    chat_id=str(i),
                    request_text="lookup and save",
                    max_iterations_override=5,
                )
                assert writes == [(100 + i) * 3]
                assert str((100 + i) * 3) in result.reply
                assert store.unresolved(result.execution_id) == []
                return {
                    "tools": result.tools_used,
                    "reply": result.reply,
                    "writes": len(writes),
                    "injected_failures": observed.injected,
                    "requests": observed.requests,
                    "execution_status": store.recent()[0]["status"],
                }
            finally:
                store.close()
                await registry.shutdown()

        await record(f"tool_chain_and_recovery_{index}", tool_case)

    judge = ProactiveJudge(ObservedClient(client), model=STEP5_MODEL, max_tokens=2048)
    for i, (topic, rule, expected) in enumerate(
        [
            (
                "新 Akasha2 消费与恢复设计发布",
                "请主动分享 Akasha2 的实质更新，尤其消费游标和崩溃恢复；这是我正在做的项目。",
                True,
            ),
            ("今日影视明星穿搭热榜", "不要分享娱乐明星或穿搭新闻。", False),
            ("某电商平台促销", "不要分享广告、电商促销；只关心 Agent Runtime。", False),
            (
                "Agent 工具重复执行故障的修复方案",
                "我急需解决工具重复执行，请在有明确修复时马上告诉我。",
                True,
            ),
        ]
    ):

        async def wake_case(topic=topic, rule=rule, expected=expected, i=i):
            result = await judge.decide_content(
                [ContentContract(f"feed:{i}", topic, "public-test-fixture", "")],
                memory_text=rule,
                recent_conversation="",
                proactive_context=rule,
                current_context="用户允许当前发送，无免打扰限制。",
            )
            assert result.status == "valid" and result.send == expected
            return {
                "send": result.send,
                "cited_ids": result.cited_ids,
                "message": result.message,
                "reason": result.reason,
            }

        await record(f"wake_grounded_decision_{i}", wake_case)

    async def quality_case():
        items = [
            {
                "item_id": "feed:relevant",
                "title": "Akasha2 crash recovery",
                "published_at": "2026-10-19T00:00:00Z",
            },
            {
                "item_id": "feed:excluded",
                "title": "Celebrity clothing advertising",
                "published_at": "2026-10-19T00:00:00Z",
            },
        ]
        scores = await judge.score_content(
            items,
            "The user explicitly asks for Akasha2 recovery updates and excludes celebrity and advertising news.",
        )
        assert scores["feed:relevant"].interest > scores["feed:excluded"].interest
        assert not scores["feed:excluded"].eligible
        from dataclasses import asdict

        return {key: asdict(value) for key, value in scores.items()}

    await record("wake_interest_estimates", quality_case)

    async def full_runtime():
        work = a.workspace / "runtime"
        work.mkdir()
        # Override provider environment within this isolated verifier process.
        for key in (
            "LIGHT_MODEL_ID",
            "LIGHT_OPENAI_COMPATIBLE_BASE_URL",
            "LIGHT_OPENAI_COMPATIBLE_API_KEY",
        ):
            os.environ.pop(key, None)
        os.environ["MODEL_ID"] = STEP5_MODEL
        os.environ["OPENAI_COMPATIBLE_BASE_URL"] = STEP_PLAN_BASE_URL
        os.environ["OPENAI_COMPATIBLE_API_KEY"] = a.key_file.read_text().strip()
        (work / "config.toml").write_text(
            '[llm.main]\nmodel="step-5-preview"\nbase_url="https://api.stepfun.com/step_plan/v1"\nenable_thinking=false\n[agent]\nmax_iterations=8\n[memory]\nenabled=false\n'
        )
        nonce = "KIRA_" + uuid4().hex[:12]
        runtime = await build_runtime(work)
        try:
            first = await runtime.process_direct(
                f"请用工具把字符串 {nonce} 写入文件 probe.txt，然后读取文件核实。",
                session_key="verify:durable",
            )
            assert (work / "probe.txt").read_text().strip() == nonce
            assert runtime.service_registry.catalog()
        finally:
            await runtime.stop_background([])
        runtime = await build_runtime(work)
        try:
            second = await runtime.process_direct(
                "上一轮我让你写入文件的完整字符串是什么？只输出该字符串。",
                session_key="verify:durable",
            )
            assert nonce in second.content
            return {
                "first_reply": first.content,
                "restart_reply": second.content,
                "persisted_messages": len(
                    runtime.session_manager.get_or_create("verify:durable").messages
                ),
                "capabilities": runtime.service_registry.catalog(),
            }
        finally:
            await runtime.stop_background([])

    await record("production_pipeline_files_and_restart", full_runtime)
    report["passed"] = sum(c["status"] == "passed" for c in report["cases"])
    report["total"] = len(report["cases"])
    a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if report["passed"] != report["total"]:
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key-file", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(verify(p.parse_args()))


if __name__ == "__main__":
    main()
