"""Version check between background reasoning and outward delivery."""

from dataclasses import dataclass


class DecisionSuperseded(RuntimeError):
    """Inputs changed: preserve candidates and start a fresh decision later."""


@dataclass(frozen=True)
class AdmissionStamp:
    session_head: tuple[int, str] | None
    rules: str
