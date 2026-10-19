"""Schedule notify/run work in its creating session, with durable fire receipts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.composition import ResourceScope
from agent.model_runtime.types import ModelRequestError
from agent.tools.registry import object_schema
from bus.events import InboundMessage, OutboundMessage
from bus.queue import OutboundDeliveryError
from core.schema import ToolSpec

from .store import Fire, Job, JobStore
from .timing import advance, is_cron_expr, next_cron_fire, parse_duration, parse_when_at


class SchedulerService:
    MAX_ACTIVE_JOBS = 10
    _DISABLED_TOOLS = [
        "memorize",
        "remember_memory",
        "forget_memory",
        "reinforce_memory",
        "compact",
    ]

    def __init__(self, path: Path, *, bus, tools, pipeline, sessions, executions=None):
        self.bus, self.tools, self.pipeline, self.sessions = (
            bus,
            tools,
            pipeline,
            sessions,
        )
        self.executions = executions
        self.store = JobStore(path)
        self._wake = asyncio.Event()
        self._running = False
        self._tasks = {}
        self._scope = ResourceScope()
        self._recover()
        self._register_tools()

    def _recover(self):
        now = datetime.now(timezone.utc)
        state = self.store.read()
        if not state.jobs and not state.fires:
            return

        def recover(current):
            for key, fire in list(current.fires.items()):
                if fire.status in {"running", "delivering"}:
                    error = "runtime stopped after fire execution began; external results require reconciliation"
                    current.fires[key] = fire.model_copy(
                        update={"status": "needs_reconciliation", "error": error}
                    )
                    job = current.jobs[fire.job.id]
                    if job.status != "cancelled":
                        current.jobs[job.id] = job.model_copy(
                            update={
                                "status": "needs_reconciliation",
                                "last_error": error,
                            }
                        )
            for key, job in list(current.jobs.items()):
                if job.status != "pending" or job.run_at > now:
                    continue
                if job.fire_key in current.fires:
                    continue  # a queued fire has not begun; do not skip it as a misfire
                next_time = advance(job, now)
                if next_time is not None and (
                    job.remaining_runs > 1 or job.remaining_runs == -1
                ):
                    current.jobs[key] = job.model_copy(update={"run_at": next_time})
                elif (now - job.run_at).total_seconds() > 300:
                    current.jobs[key] = job.model_copy(
                        update={
                            "status": "missed",
                            "last_error": "missed while runtime was offline",
                        }
                    )

        self.store.change(recover)

    async def schedule(
        self,
        text: str,
        trigger: str,
        when: str,
        action: str = "notify",
        context: str = "none",
        timezone: str = "",
        name: str = "",
    ):
        origin = self.tools.context
        session_key = str(origin.get("session_key") or "")
        channel, chat_id = (
            str(origin.get("channel") or ""),
            str(origin.get("chat_id") or ""),
        )
        if not all((session_key, channel, chat_id)):
            return (
                "Error: schedule requires a creating session and channel/chat context"
            )
        if action not in {"notify", "run"} or context not in {"none", "session"}:
            return "Error: action must be notify/run and context must be none/session"
        text, trigger, when = text.strip(), trigger.strip(), when.strip()
        if not text:
            return "Error: text is required"
        tz = timezone.strip() or str(origin.get("timezone") or "UTC")
        now = datetime.now(ZoneInfo("UTC"))
        interval, cron, runs = 0, "", 1
        try:
            ZoneInfo(tz)
            if trigger == "at":
                run_at = parse_when_at(when, tz, now=now)
            elif trigger == "after":
                run_at = now + parse_duration(when)
            elif trigger == "every":
                runs = -1
                if is_cron_expr(when):
                    cron = when
                    run_at = next_cron_fire(cron, tz, now)
                else:
                    interval = int(parse_duration(when).total_seconds())
                    if interval <= 0:
                        raise ValueError("recurrence must be positive")
                    run_at = now + timedelta(seconds=interval)
            else:
                raise ValueError("trigger must be at, after, or every")
            if run_at <= now:
                raise ValueError("scheduled time must be in the future")
            job = Job(
                id="job_" + uuid4().hex[:12],
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                action=action,
                text=text,
                context=context,
                run_at=run_at.astimezone(ZoneInfo("UTC")),
                trigger=trigger,
                interval_seconds=interval,
                cron_expr=cron,
                remaining_runs=runs,
                created_at=now,
                timezone=tz,
                name=name.strip(),
            )
        except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
            return "Error: " + str(exc)
        if (
            sum(j.status == "pending" for j in self.store.read().jobs.values())
            >= self.MAX_ACTIVE_JOBS
        ):
            return "Error: schedule_capacity_reached"
        self.store.change(lambda state: state.jobs.update({job.id: job}))
        self._wake.set()
        return job.model_dump_json()

    async def list_schedules(self, include_finished: bool = False):
        key = str(self.tools.context.get("session_key") or "")
        jobs = [
            job
            for job in self.store.read().jobs.values()
            if job.session_key == key and (include_finished or job.status == "pending")
        ]
        return json.dumps(
            [j.model_dump(mode="json") for j in sorted(jobs, key=lambda j: j.run_at)],
            ensure_ascii=False,
        )

    async def cancel_schedule(self, job_id: str):
        state = self.store.read()
        job = state.jobs.get(job_id)
        key = str(self.tools.context.get("session_key") or "")
        if job is None or job.session_key != key:
            return "Error: schedule not found in this session"
        if job.status not in {"pending", "needs_reconciliation"}:
            return "Error: schedule is already " + job.status
        self.store.change(
            lambda state: state.jobs.update(
                {job.id: job.model_copy(update={"status": "cancelled"})}
            )
        )
        # A task may cancel itself through a model tool. Persist the cancellation
        # before injecting it, and let pipeline/exec finally blocks settle first.
        task = self._tasks.get(job.id)
        if task is not None and not task.done():
            task.cancel()
        self._wake.set()
        return "Cancelled schedule " + job.id

    def _phase(self, key, status, **values):
        def update(state):
            state.fires[key] = state.fires[key].model_copy(
                update={"status": status, **values}
            )

        self.store.change(update)

    def _settle(self, key, status, error="", output_id=None):
        def update(state):
            fire = state.fires[key]
            state.fires[key] = fire.model_copy(
                update={
                    "status": status,
                    "error": error,
                    "output_id": output_id or fire.output_id,
                }
            )
            job = state.jobs[fire.job.id]
            if job.status == "cancelled":
                return
            remaining = (
                job.remaining_runs - 1 if job.remaining_runs > 0 else job.remaining_runs
            )
            next_time = (
                advance(job, datetime.now(ZoneInfo("UTC")))
                if status in {"completed", "failed"} and remaining != 0
                else None
            )
            state.jobs[job.id] = job.model_copy(
                update={
                    "remaining_runs": remaining,
                    "status": "pending" if next_time is not None else status,
                    "run_at": next_time or job.run_at,
                    "last_error": error,
                }
            )

        self.store.change(update)

    def _claim(self, job_id):
        candidate = self.store.read().jobs.get(job_id)
        if (
            candidate is None
            or candidate.status != "pending"
            or candidate.run_at > datetime.now(ZoneInfo("UTC"))
        ):
            return None

        def update(state):
            job = state.jobs[job_id]
            if job.status != "pending" or job.run_at > datetime.now(ZoneInfo("UTC")):
                return None
            existing = state.fires.get(job.fire_key)
            if existing is not None:
                return existing if existing.status == "pending" else None
            fire = Fire(job=job)
            state.fires[job.fire_key] = fire
            return fire

        return self.store.change(update)

    async def _record_failure(self, fire, error):
        async with self.pipeline.admission.hold(fire.job.session_key):
            session = self.sessions.get_or_create(fire.job.session_key)
            if not any(m["id"] == fire.input_id for m in session.messages):
                session.add_message(
                    "user",
                    fire.job.text,
                    id=fire.input_id,
                    source=fire.source,
                    skip_post_memory=True,
                )
            session.add_message(
                "assistant",
                "Scheduled task failed: " + error,
                source=fire.source,
                skip_post_memory=True,
                scheduler_failure=True,
            )
            self.sessions.save(session)

    async def _notify(self, fire):
        async with self.pipeline.admission.hold(fire.job.session_key):
            self._phase(
                fire.job.fire_key, "running", started_at=datetime.now(ZoneInfo("UTC"))
            )
            session = self.sessions.get_or_create(fire.job.session_key)
            message = session.add_message(
                "assistant",
                fire.job.text,
                id=fire.notification_id,
                source=fire.source,
                skip_post_memory=True,
                scheduled_job_id=fire.job.id,
            )
            self.sessions.save(session)
            return OutboundMessage(
                fire.job.channel,
                fire.job.chat_id,
                message["content"],
                session_message_id=message["id"],
                metadata={"source": fire.source, "scheduled_job_id": fire.job.id},
            )

    def _unknown_outcome(self, fire):
        return fire.status == "delivering" or (
            fire.status == "running"
            and (
                self.executions is None
                or fire.started_at is None
                or self.executions.needs_reconciliation_since(
                    fire.job.session_key, fire.started_at.isoformat()
                )
            )
        )

    async def fire(self, job_id):
        current_task = self._tasks.get(job_id)
        if current_task is not None and current_task is not asyncio.current_task():
            return
        fire = self._claim(job_id)
        if fire is None:
            return
        key = fire.job.fire_key
        self._tasks[job_id] = asyncio.current_task()
        try:
            if fire.job.action == "notify":
                outbound = await self._notify(fire)
            else:
                if not any(
                    s["key"] == fire.job.session_key
                    for s in self.sessions.list_sessions()
                ):
                    raise ValueError("creating session no longer exists")
                inbound = InboundMessage(
                    fire.job.channel,
                    "scheduler:" + fire.job.id,
                    fire.job.chat_id,
                    fire.job.text,
                    metadata={
                        "session_key_override": fire.job.session_key,
                        "source": fire.source,
                        "input_message_id": fire.input_id,
                        "skip_post_memory": True,
                        "history_scope": fire.job.context,
                        "skip_memory_retrieval": fire.job.context == "none",
                        "disabled_tools": self._DISABLED_TOOLS,
                        "scheduled_job_id": fire.job.id,
                    },
                )
                outbound = await self.pipeline.run(
                    inbound,
                    fire.job.session_key,
                    dispatch_outbound=False,
                    admitted=lambda: self._phase(
                        key, "running", started_at=datetime.now(ZoneInfo("UTC"))
                    ),
                )
                if not outbound.session_message_id:
                    raise ModelRequestError(
                        "scheduled task returned without a committed output"
                    )
                if outbound.metadata.get("execution_status") == "budget_exhausted":
                    raise ModelRequestError("scheduled task exhausted its step budget")
            if self.store.read().jobs[job_id].status == "cancelled":
                self._settle(key, "cancelled", output_id=outbound.session_message_id)
                return
            self._phase(key, "delivering", output_id=outbound.session_message_id)
            await self.bus.publish_outbound_and_wait(outbound)
            self._settle(key, "completed", output_id=outbound.session_message_id)
        except ModelRequestError as exc:
            await self._record_failure(fire, str(exc))
            active = self.store.read().fires[key]
            self._settle(
                key,
                "needs_reconciliation" if self._unknown_outcome(active) else "failed",
                str(exc),
            )
        except OutboundDeliveryError as exc:
            self._settle(key, "needs_reconciliation", str(exc))
        except asyncio.CancelledError:
            current = self.store.read()
            active = current.fires[key]
            unknown = self._unknown_outcome(active)
            status = "needs_reconciliation" if unknown else "cancelled"
            self._settle(
                key, status, "execution cancelled; completed tools were not replayed"
            )
            raise
        except Exception as exc:
            active = self.store.read().fires[key]
            self._settle(
                key,
                "needs_reconciliation" if self._unknown_outcome(active) else "failed",
                str(exc),
            )
            raise
        finally:
            self._tasks.pop(job_id, None)
            self._wake.set()

    async def run(self):
        self._running = True
        try:
            while self._running:
                self._wake.clear()
                now = datetime.now(ZoneInfo("UTC"))
                for job in self.store.read().jobs.values():
                    if (
                        job.status == "pending"
                        and job.run_at <= now
                        and job.id not in self._tasks
                    ):
                        task = self._scope.spawn(
                            self.fire(job.id), name="scheduler:" + job.id
                        )
                        self._tasks[job.id] = task
                        task.add_done_callback(
                            lambda done, key=job.id: (
                                self._tasks.pop(key, None)
                                if self._tasks.get(key) is done
                                else None
                            )
                        )
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._scope.close()

    def stop(self):
        self._running = False
        self._wake.set()

    async def shutdown(self):
        self.stop()
        await self._scope.close()

    def _register_tools(self):
        common = {"source_type": "scheduler", "source_name": "scheduler"}
        self.tools.register(
            ToolSpec(
                "schedule",
                "Create a task in the current session. notify sends text verbatim; run executes tools and replies here. The final reply is delivered automatically. context=none uses only this task; session includes conversation history.",
                object_schema(
                    {
                        "text": {"type": "string"},
                        "trigger": {"type": "string", "enum": ["at", "after", "every"]},
                        "when": {
                            "type": "string",
                            "description": "ISO/HH:MM for at, 30s/5m/2h for after/every, or 5/6-field cron for every",
                        },
                        "action": {"type": "string", "enum": ["notify", "run"]},
                        "context": {"type": "string", "enum": ["none", "session"]},
                        "timezone": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    ["text", "trigger", "when"],
                ),
            ),
            self.schedule,
            risk="write",
            **common,
        )
        self.tools.register(
            ToolSpec(
                "list_schedules",
                "List tasks belonging to the current session.",
                object_schema({"include_finished": {"type": "boolean"}}, []),
            ),
            self.list_schedules,
            **common,
        )
        self.tools.register(
            ToolSpec(
                "cancel_schedule",
                "Cancel a task in the current session. Running tools drain; unknown external results are not replayed.",
                object_schema({"job_id": {"type": "string"}}, ["job_id"]),
            ),
            self.cancel_schedule,
            risk="write",
            **common,
        )
