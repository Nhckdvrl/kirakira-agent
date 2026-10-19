"""One atomic schedule snapshot owns jobs and immutable fire identities."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    session_key: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    action: Literal["notify", "run"]
    text: str = Field(min_length=1)
    context: Literal["none", "session"] = "none"
    run_at: AwareDatetime
    trigger: Literal["at", "after", "every"]
    interval_seconds: int = Field(default=0, ge=0, strict=True)
    cron_expr: str = ""
    remaining_runs: int = Field(default=1, ge=-1, strict=True)
    status: Literal[
        "pending", "completed", "cancelled", "missed", "failed", "needs_reconciliation"
    ] = "pending"
    created_at: AwareDatetime
    timezone: str = "UTC"
    name: str = ""
    last_error: str = ""

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def recurrence(self):
        if self.trigger == "every" and not (self.cron_expr or self.interval_seconds):
            raise ValueError("recurring jobs require a positive interval or cron")
        if self.status == "pending" and self.remaining_runs == 0:
            raise ValueError("pending job has no remaining runs")
        return self

    @property
    def fire_key(self):
        raw = self.id + ":" + self.run_at.astimezone(timezone.utc).isoformat()
        return hashlib.sha256(raw.encode()).hexdigest()


class Fire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job: Job
    status: Literal[
        "pending",
        "running",
        "delivering",
        "completed",
        "failed",
        "cancelled",
        "needs_reconciliation",
    ] = "pending"
    started_at: AwareDatetime | None = None
    output_id: str | None = None
    error: str = ""

    @property
    def source(self):
        return "scheduler:" + self.job.fire_key

    @property
    def input_id(self):
        return "scheduler-input:" + self.job.fire_key

    @property
    def notification_id(self):
        return "scheduler-output:" + self.job.fire_key


class ScheduleState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[2] = 2
    jobs: dict[str, Job] = Field(default_factory=dict)
    fires: dict[str, Fire] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identities(self):
        if any(key != job.id for key, job in self.jobs.items()):
            raise ValueError("job identity does not match its key")
        if any(
            key != fire.job.fire_key or fire.job.id not in self.jobs
            for key, fire in self.fires.items()
        ):
            raise ValueError("fire identity or owner is invalid")
        return self


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate schedule key: " + key)
        result[key] = value
    return result


def atomic_write(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            raw = json.loads(path.read_text(), object_pairs_hook=unique_object)
            self._state = ScheduleState.model_validate(raw)
        else:
            self._state = ScheduleState()
        self._poisoned = False

    def read(self):
        if self._poisoned:
            raise RuntimeError(
                "schedule publication failed; reopen the store before continuing"
            )
        return self._state.model_copy(deep=True)

    def change(self, change):
        state = self.read()
        result = change(state)
        state = ScheduleState.model_validate(state.model_dump())
        data = state.model_dump_json(indent=2).encode() + b"\n"
        try:
            atomic_write(self.path, data)
        except BaseException:
            # Publication may have reached replace before a failure. Never guess
            # whether the old in-memory state still represents the durable file.
            self._poisoned = True
            raise
        self._state = state
        return result
