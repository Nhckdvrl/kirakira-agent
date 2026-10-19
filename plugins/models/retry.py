"""One bounded, cancellable recovery policy for a logical model request."""

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def retry_after_seconds(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            seconds = (target - (now or datetime.now(UTC))).total_seconds()
        except ValueError, TypeError, OverflowError:
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


@dataclass(frozen=True)
class ModelRetryPolicy:
    max_retries: int = 10
    initial_delay: float = 0.5
    max_delay: float = 32.0
    jitter: float = 0.25

    def __post_init__(self):
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 100:
            raise ValueError("max_retries must be an integer between 0 and 100")
        if any(
            not math.isfinite(v) or v < 0
            for v in (self.initial_delay, self.max_delay, self.jitter)
        ):
            raise ValueError("retry delays and jitter must be finite and nonnegative")

    def delay(self, retry: int, server_delay: float | None) -> float:
        if (
            server_delay is not None
            and math.isfinite(server_delay)
            and server_delay >= 0
        ):
            return server_delay
        base = min(self.max_delay, self.initial_delay * 2 ** min(retry - 1, 20))
        return base * (1 + random.uniform(0, self.jitter))
