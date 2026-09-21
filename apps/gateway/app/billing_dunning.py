"""Durable, idempotent dunning schedule decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class DunningPolicy:
    grace_days: int = 7
    suspend_days: int = 21

    def __post_init__(self) -> None:
        if self.grace_days < 1 or self.suspend_days <= self.grace_days:
            raise ValueError("invalid_dunning_policy")


@dataclass(frozen=True)
class DunningDecision:
    state: str
    next_attempt_at: datetime | None
    event_key: str


def decide(*, invoice_id: str, due_at: datetime, now: datetime, attempt: int, policy: DunningPolicy) -> DunningDecision:
    overdue_days = max(0, (now - due_at).days)
    if overdue_days >= policy.suspend_days:
        state = "SUSPENDED"
    elif overdue_days >= policy.grace_days:
        state = "GRACE_PERIOD"
    else:
        state = "PAST_DUE"
    next_attempt_at = None if state == "SUSPENDED" else now + timedelta(days=max(1, attempt))
    return DunningDecision(state=state, next_attempt_at=next_attempt_at, event_key=f"dunning:{invoice_id}:{state}:{attempt}")
