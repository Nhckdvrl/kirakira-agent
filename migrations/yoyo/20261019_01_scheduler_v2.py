"""Migrate the old scheduler snapshot once, with an exact pre-v2 backup."""

import json
import os
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from yoyo import step

from agent.migrations.context import current_migration_context

__depends__ = {"20261002_01_message_publications"}


# Frozen legacy schema: migration validation must not drift with runtime models.
class LegacyJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    message: str = ""
    prompt: str = ""
    tier: Literal["instant", "soft"] = "instant"
    run_at: AwareDatetime
    created_at: AwareDatetime | Literal[""] = ""
    interval_seconds: int = Field(default=0, ge=0, strict=True)
    remaining_runs: int = Field(default=1, ge=-1, strict=True)
    status: Literal["pending", "completed", "cancelled", "missed", "failed"] = "pending"
    trigger: Literal["at", "after", "every"] = "at"
    cron_expr: str = ""
    timezone: str = "UTC"
    name: str = ""
    last_error: str = ""

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def valid_task(self):
        if not (self.prompt if self.tier == "soft" else self.message).strip():
            raise ValueError("empty legacy schedule")
        if self.status == "pending" and self.remaining_runs == 0:
            raise ValueError("pending legacy schedule has no remaining runs")
        if self.trigger == "every" and not (self.cron_expr or self.interval_seconds):
            raise ValueError("legacy recurrence requires an interval or cron")
        if self.cron_expr:
            parts = self.cron_expr.split()
            names = ("minute", "hour", "day", "month", "day_of_week")
            if len(parts) == 6:
                names = ("second",) + names
            if len(parts) != len(names):
                raise ValueError("invalid legacy cron")
            CronTrigger(timezone=self.timezone, **dict(zip(names, parts)))
        return self


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate legacy schedule key")
        result[key] = value
    return result


def migrate_schedules(_connection):
    path = current_migration_context().workspace / ".kirakira" / "schedules.json"
    if not path.exists():
        return
    original = path.read_bytes()
    raw = json.loads(original, object_pairs_hook=unique_object)
    if isinstance(raw, dict) and raw.get("version") == 2:
        return
    if raw == []:
        # Older setup wizard created this empty sentinel before any task existed.
        raw = {"jobs": []}
    if (
        not isinstance(raw, dict)
        or set(raw) != {"jobs"}
        or not isinstance(raw["jobs"], list)
    ):
        raise ValueError("invalid legacy schedules; original file was not changed")
    jobs = {}
    for item in raw["jobs"]:
        validated = LegacyJob.model_validate(item)
        old = validated.model_dump(mode="json")
        key = validated.id
        if key in jobs:
            raise ValueError("duplicate legacy job identity")
        action = "run" if validated.tier == "soft" else "notify"
        job = dict(old)
        for name in ("tier", "message", "prompt"):
            job.pop(name)
        job.update(
            session_key=validated.channel + ":" + validated.chat_id,
            action=action,
            text=validated.prompt if action == "run" else validated.message,
            context="none",
            created_at=old["created_at"] or old["run_at"],
        )
        jobs[key] = job
    payload = (
        json.dumps(
            {"version": 2, "jobs": jobs, "fires": {}}, ensure_ascii=False, indent=2
        )
        + "\n"
    ).encode()
    backup = path.with_name(path.name + ".pre-v2-" + uuid4().hex)
    with backup.open("xb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    temp = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


steps = [step(migrate_schedules)]
