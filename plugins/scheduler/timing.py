"""Timezone-aware schedule parsing and phase-preserving recurrence."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

_DURATION_PARTS = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(value: str) -> timedelta:
    matched = _DURATION_PARTS.fullmatch(value.strip())
    if matched is None or not any(matched.groups()):
        raise ValueError("invalid duration; examples: 30s, 5m, 2h, 1h30m")
    days, hours, minutes, seconds = (int(item or 0) for item in matched.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def is_cron_expr(value: str) -> bool:
    return len(value.strip().split()) in {5, 6}


def next_cron_fire(value: str, tz: str, after: datetime) -> datetime:
    parts = value.strip().split()
    if len(parts) not in {5, 6}:
        raise ValueError("cron expression must contain 5 or 6 fields")
    zone = ZoneInfo(tz)
    names = (
        ("minute", "hour", "day", "month", "day_of_week")
        if len(parts) == 5
        else ("second", "minute", "hour", "day", "month", "day_of_week")
    )
    try:
        trigger = CronTrigger(timezone=zone, **dict(zip(names, parts)))
        result = trigger.get_next_fire_time(None, after.astimezone(zone))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid cron expression: %s" % value) from exc
    if result is None:
        raise ValueError("cron expression has no next fire time: %s" % value)
    return result


def parse_when_at(value: str, tz: str, *, now: datetime | None = None) -> datetime:
    zone = ZoneInfo(tz)
    current = (now or datetime.now(zone)).astimezone(zone)
    text = value.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        parsed = datetime.strptime(text, "%H:%M")
        result = current.replace(
            hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
        )
        return result + timedelta(days=1) if result <= current else result
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed


def advance(job, after: datetime) -> datetime | None:
    if job.cron_expr:
        return next_cron_fire(
            job.cron_expr, job.timezone, after + timedelta(microseconds=1)
        ).astimezone(timezone.utc)
    if job.interval_seconds > 0:
        elapsed = max(0, int((after - job.run_at).total_seconds()))
        return job.run_at + timedelta(
            seconds=(elapsed // job.interval_seconds + 1) * job.interval_seconds
        )
    return None
