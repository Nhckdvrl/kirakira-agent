"""Real Step5 Drift programs, local channel receipts, durable proposal outcomes."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from agent.core.execution import ExecutionStore
from bus.queue import MessageBus
from infra.providers.step_plan import StepPlanClient, STEP5_MODEL, STEP_PLAN_BASE_URL
from plugins.drift_flow.runner import DriftRunner
from proactive_v2.config import DriftConfig
from session.manager import SessionManager


async def verify(a):
    client = StepPlanClient(
        api_key=a.key_file.read_text().strip(), thinking_enabled=False, timeout=120
    )
    cases = []
    for kind in ("silent", "delivered", "superseded", "paused"):
        path = a.workspace / kind
        skill = path / "drift/skills/verify"
        skill.mkdir(parents=True)
        instructions = {
            "silent": 'Append a progress journal note "verified silent work", then finish_drift with completed status and briefing. Do not send a message.',
            "delivered": 'Call message_push with "KIRAKIRA_DRIFT_OK". Then call finish_drift separately with completed status and briefing. After message_push only finish_drift is allowed.',
            "superseded": 'Call message_push with "KIRAKIRA_DRIFT_OK". Then call finish_drift separately with completed status and briefing.',
            "paused": 'Call finish_drift with status paused, briefing "needs next pass", scratchpad_update "continue from verified fixture". Do not send a message.',
        }
        (skill / "SKILL.md").write_text(
            "---\nname: verify\ndescription: deterministic local verification\n---\n"
            "You are verifying a local background program. Use only journal_append, message_push, finish_drift. "
            "Never use shell, file, web, scheduling or network tools. "
            + instructions[kind]
        )
        sessions = SessionManager(path)
        session = sessions.get_or_create("verify:drift")
        session.add_message("user", "Run the local verification fixture.")
        sessions.save(session)
        bus = MessageBus()
        received = []

        async def receive(message):
            received.append(message.content)

        bus.subscribe_outbound("verify", receive)
        dispatch = asyncio.create_task(bus.dispatch_outbound())
        store = ExecutionStore(path / "executions.db")
        runner = DriftRunner(
            config=DriftConfig(enabled=True, min_interval_hours=0, max_steps=8),
            workspace=path,
            bus=bus,
            session_manager=sessions,
            model_client=client,
            model=STEP5_MODEL,
            target_channel="verify",
            target_chat_id="drift",
            max_tokens=2048,
            execution_store=store,
        )
        runner._hazard_due = lambda *args: (
            True
        )  # Timer only; execution and settlement are real.
        runner._select_skill = lambda skills: next(
            s for s in skills if s.name == "verify"
        )
        if kind == "superseded":
            original = runner._run_agent

            async def change_head(*args):
                await original(*args)
                session.add_message(
                    "user", "New user activity supersedes the pending draft."
                )
                sessions.save(session)

            runner._run_agent = change_head
        try:
            ran = await runner.maybe_run(datetime.now(timezone.utc), "verify:drift")
            proposal = runner._state.recent_proposals()[0]
            expected = {
                "silent": "completed",
                "delivered": "completed",
                "superseded": "superseded",
                "paused": "paused",
            }[kind]
            assert ran and proposal["status"] == expected
            assert received == (["KIRAKIRA_DRIFT_OK"] if kind == "delivered" else [])
            cases.append(
                {
                    "name": kind,
                    "status": "passed",
                    "proposal_status": proposal["status"],
                    "received": received,
                    "runs": runner._state.recent_runs(),
                    "executions": [
                        {k: r[k] for k in ("scope", "status")} for r in store.recent()
                    ],
                }
            )
        except Exception as exc:
            cases.append(
                {"name": kind, "status": "failed", "error": type(exc).__name__}
            )
        finally:
            runner.close()
            store.close()
            sessions.close()
            bus.stop()
            await bus.shutdown()
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)
        print(json.dumps(cases[-1]), flush=True)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(
            json.dumps(
                {
                    "endpoint": STEP_PLAN_BASE_URL,
                    "model": STEP5_MODEL,
                    "billing_surface": "step_plan",
                    "cases": cases,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if any(c["status"] != "passed" for c in cases):
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key-file", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(verify(p.parse_args()))


if __name__ == "__main__":
    main()
