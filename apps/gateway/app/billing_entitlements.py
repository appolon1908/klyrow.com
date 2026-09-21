"""Provider-neutral subscription state and entitlement calculations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class SubscriptionState(StrEnum):
    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    GRACE_PERIOD = "GRACE_PERIOD"
    CANCEL_AT_PERIOD_END = "CANCEL_AT_PERIOD_END"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


LEGAL_TRANSITIONS: dict[SubscriptionState, frozenset[SubscriptionState]] = {
    SubscriptionState.TRIALING: frozenset({SubscriptionState.ACTIVE, SubscriptionState.CANCELLED}),
    SubscriptionState.ACTIVE: frozenset({SubscriptionState.PAST_DUE, SubscriptionState.CANCEL_AT_PERIOD_END, SubscriptionState.SUSPENDED}),
    SubscriptionState.PAST_DUE: frozenset({SubscriptionState.ACTIVE, SubscriptionState.GRACE_PERIOD, SubscriptionState.SUSPENDED}),
    SubscriptionState.GRACE_PERIOD: frozenset({SubscriptionState.ACTIVE, SubscriptionState.SUSPENDED}),
    SubscriptionState.CANCEL_AT_PERIOD_END: frozenset({SubscriptionState.ACTIVE, SubscriptionState.CANCELLED}),
    SubscriptionState.SUSPENDED: frozenset({SubscriptionState.ACTIVE, SubscriptionState.CANCELLED}),
    SubscriptionState.CANCELLED: frozenset({SubscriptionState.ACTIVE, SubscriptionState.CLOSED}),
    SubscriptionState.CLOSED: frozenset(),
}


@dataclass(frozen=True)
class SubscriptionSnapshot:
    state: SubscriptionState
    version: int
    period_end: datetime
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False


def transition(snapshot: SubscriptionSnapshot, target: SubscriptionState) -> SubscriptionSnapshot:
    if target not in LEGAL_TRANSITIONS[snapshot.state]:
        raise ValueError(f"invalid_subscription_transition:{snapshot.state}->{target}")
    return SubscriptionSnapshot(
        state=target,
        version=snapshot.version + 1,
        period_end=snapshot.period_end,
        trial_end=snapshot.trial_end,
        cancel_at_period_end=target is SubscriptionState.CANCEL_AT_PERIOD_END,
    )


def require_version(snapshot: SubscriptionSnapshot, expected_version: int) -> None:
    if snapshot.version != expected_version:
        raise ValueError("subscription_version_conflict")


QUOTA_ENTITLEMENTS = frozenset({"seats", "domains", "profiles", "messages"})


def _quota(limit: int, used: int) -> dict[str, int]:
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
    }


def _usage(usage: Mapping[str, int], key: str) -> int:
    value = usage.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _api_entitlement(value: object, usage: Mapping[str, int]) -> object:
    if isinstance(value, Mapping):
        enabled = value.get("enabled", True)
        result: dict[str, object] = {"enabled": enabled is True}
        requests = value.get("requests")
        if isinstance(requests, int) and not isinstance(requests, bool):
            result["requests"] = _quota(requests, _usage(usage, "api_requests"))
        return result
    return value


def calculate_entitlements(
    *,
    state: SubscriptionState,
    features: Mapping[str, object],
    usage: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """Return effective features without mutating financial history.

    Standard plan features use ``seats``, ``domains``, ``profiles``, and
    ``messages`` as consumable quotas. ``api`` accepts either a boolean or an
    object with ``enabled`` and a request quota. ``retention_days`` is a
    non-consumable policy value.
    """
    usage = usage or {}
    if state in {SubscriptionState.SUSPENDED, SubscriptionState.CANCELLED, SubscriptionState.CLOSED}:
        return {key: False for key in features}
    result: dict[str, object] = dict(features)
    for key, value in features.items():
        if key in QUOTA_ENTITLEMENTS and isinstance(value, int) and not isinstance(value, bool):
            result[key] = _quota(value, _usage(usage, key))
        elif key == "api":
            result[key] = _api_entitlement(value, usage)
        elif key == "retention_days" and isinstance(value, int) and not isinstance(value, bool):
            result[key] = {"days": value}
        elif isinstance(value, int) and not isinstance(value, bool):
            result[key] = _quota(value, _usage(usage, key))
    return result
