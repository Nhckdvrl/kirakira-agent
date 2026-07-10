"""Persistent user-requested delayed message scheduler."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from kirakira_agent.bus import MessageBus
from kirakira_agent.events import OutboundMessage
from kirakira_agent.schema import ToolSpec
from kirakira_agent.tools.registry import ToolRegistry, object_schema


@dataclass
class ScheduledMessage:
    id: str
    channel: str
    chat_id: str
    message: str
    run_at: str
    interval_seconds: int = 0
    remaining_runs: int = 1
    status: str = "pending"
    created_at: str = ""
    last_error: str = ""


class SchedulerService:
    def __init__(
        self,
        path: Path,
        *,
        bus: MessageBus,
        tools: ToolRegistry,
    ) -> None:
        self.path = path
        self.bus = bus
        self.tools = tools
        self._jobs: Dict[str, ScheduledMessage] = {}
        self._wake = asyncio.Event()
        self._running = False
        self._load()
        self._register_tools()

    async def run(self) -> None:
        self._running = True
        while self._running:
            due = self._due_jobs()
            for job in due:
                await self._fire(job)
            timeout = self._seconds_until_next()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    async def schedule(
        self,
        message: str,
        run_at: str = "",
        delay_seconds: int = 0,
        interval_seconds: int = 0,
        repeat_count: int = 1,
    ) -> str:
        context = self.tools.context
        channel = str(context.get("channel") or "").strip()
        chat_id = str(context.get("chat_id") or "").strip()
        if not channel or not chat_id:
            return "Error: schedule requires an active channel/chat context"
        message = message.strip()
        if not message:
            return "Error: scheduled message is empty"
        if run_at:
            try:
                when = datetime.fromisoformat(run_at)
            except ValueError:
                return "Error: run_at must be an ISO-8601 datetime"
            if when.tzinfo is None:
                when = when.astimezone()
        elif delay_seconds > 0:
            when = datetime.now().astimezone() + timedelta(seconds=int(delay_seconds))
        else:
            return "Error: provide run_at or a positive delay_seconds"
        if when <= datetime.now().astimezone():
            return "Error: scheduled time must be in the future"
        interval = max(0, int(interval_seconds))
        repeats = max(1, min(1000, int(repeat_count)))
        if repeats > 1 and interval <= 0:
            return "Error: interval_seconds is required when repeat_count > 1"
        job = ScheduledMessage(
            id="job_%s" % uuid4().hex[:12],
            channel=channel,
            chat_id=chat_id,
            message=message,
            run_at=when.isoformat(),
            interval_seconds=interval,
            remaining_runs=repeats,
            created_at=datetime.now().astimezone().isoformat(),
        )
        self._jobs[job.id] = job
        self._save()
        self._wake.set()
        return json.dumps(asdict(job), ensure_ascii=False)

    def list_schedules(self, include_finished: bool = False) -> str:
        jobs = [
            asdict(job)
            for job in self._jobs.values()
            if include_finished or job.status == "pending"
        ]
        jobs.sort(key=lambda item: item["run_at"])
        return json.dumps(jobs, ensure_ascii=False, indent=2)

    def cancel_schedule(self, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if job is None:
            return "Error: schedule not found: %s" % job_id
        if job.status != "pending":
            return "Error: schedule is already %s" % job.status
        job.status = "cancelled"
        self._save()
        self._wake.set()
        return "Cancelled schedule %s" % job_id

    async def _fire(self, job: ScheduledMessage) -> None:
        try:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=job.channel,
                    chat_id=job.chat_id,
                    content=job.message,
                    metadata={"scheduled_job_id": job.id},
                )
            )
            job.remaining_runs -= 1
            if job.remaining_runs > 0 and job.interval_seconds > 0:
                next_time = datetime.fromisoformat(job.run_at) + timedelta(
                    seconds=job.interval_seconds
                )
                now = datetime.now().astimezone()
                while next_time <= now:
                    next_time += timedelta(seconds=job.interval_seconds)
                job.run_at = next_time.isoformat()
            else:
                job.status = "completed"
        except Exception as exc:
            job.last_error = str(exc)
            job.status = "failed"
        self._save()

    def _due_jobs(self) -> List[ScheduledMessage]:
        now = datetime.now().astimezone()
        return [
            job
            for job in self._jobs.values()
            if job.status == "pending" and datetime.fromisoformat(job.run_at) <= now
        ]

    def _seconds_until_next(self) -> float:
        pending = [
            datetime.fromisoformat(job.run_at)
            for job in self._jobs.values()
            if job.status == "pending"
        ]
        if not pending:
            return 60.0
        return max(0.05, min(60.0, (min(pending) - datetime.now().astimezone()).total_seconds()))

    def _register_tools(self) -> None:
        self.tools.register(
            ToolSpec(
                "schedule",
                "Schedule a message for the current channel/chat at an ISO time or after a delay.",
                object_schema(
                    {
                        "message": {"type": "string"},
                        "run_at": {"type": "string"},
                        "delay_seconds": {"type": "integer"},
                        "interval_seconds": {"type": "integer"},
                        "repeat_count": {"type": "integer"},
                    },
                    ["message"],
                ),
            ),
            self.schedule,
        )
        self.tools.register(
            ToolSpec(
                "list_schedules",
                "List scheduled messages for all channels.",
                object_schema({"include_finished": {"type": "boolean"}}, []),
            ),
            self.list_schedules,
        )
        self.tools.register(
            ToolSpec(
                "cancel_schedule",
                "Cancel a pending scheduled message by id.",
                object_schema({"job_id": {"type": "string"}}, ["job_id"]),
            ),
            self.cancel_schedule,
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
            for item in jobs:
                if isinstance(item, dict) and item.get("id"):
                    job = ScheduledMessage(**item)
                    self._jobs[job.id] = job
        except (OSError, ValueError, TypeError):
            self._jobs = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(".%s.%s.tmp" % (self.path.name, uuid4().hex))
        try:
            temp.write_text(
                json.dumps(
                    {"jobs": [asdict(job) for job in self._jobs.values()]},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
