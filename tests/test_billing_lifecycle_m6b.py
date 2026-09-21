from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apps.gateway.app.billing_dunning import DunningPolicy, decide
from apps.gateway.app.billing_entitlements import (
    SubscriptionSnapshot,
    SubscriptionState,
    calculate_entitlements,
    require_version,
    transition,
)
from apps.gateway.app.billing_proration import quote_plan_change


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 2, 1, tzinfo=UTC)


def snapshot(state=SubscriptionState.ACTIVE, version=1):
    return SubscriptionSnapshot(state=state, version=version, period_end=END)


def test_legal_transition_increments_version_and_sets_cancel_flag():
    cancelled = transition(snapshot(), SubscriptionState.CANCEL_AT_PERIOD_END)
    assert cancelled.version == 2
    assert cancelled.cancel_at_period_end is True
    assert transition(cancelled, SubscriptionState.CANCELLED).version == 3


def test_illegal_transition_and_stale_version_fail_closed():
    with pytest.raises(ValueError, match="invalid_subscription_transition"):
        transition(snapshot(), SubscriptionState.CLOSED)
    with pytest.raises(ValueError, match="subscription_version_conflict"):
        require_version(snapshot(version=3), 2)


def test_suspended_subscription_loses_entitlements_without_financial_mutation():
    assert calculate_entitlements(
        state=SubscriptionState.ACTIVE,
        features={"send": True, "messages": 1000},
        usage={"messages": 125},
    ) == {"send": True, "messages": {"limit": 1000, "used": 125, "remaining": 875}}
    assert calculate_entitlements(
        state=SubscriptionState.SUSPENDED,
        features={"send": True, "messages": 1000},
    ) == {"send": False, "messages": False}


def test_proration_is_deterministic_and_rounds_half_up():
    quote = quote_plan_change(
        old_price=Decimal("10.00"),
        new_price=Decimal("20.00"),
        period_start=START,
        period_end=END,
        at=datetime(2026, 1, 16, tzinfo=UTC),
    )
    assert quote.charge == Decimal("5.16")
    assert quote.credit == Decimal("0.00")
    assert quote.effective == "IMMEDIATE"


def test_downgrade_is_next_period_and_dunning_event_is_idempotency_keyed():
    quote = quote_plan_change(
        old_price=Decimal("20.00"),
        new_price=Decimal("10.00"),
        period_start=START,
        period_end=END,
        at=datetime(2026, 1, 16, tzinfo=UTC),
    )
    assert quote.effective == "NEXT_PERIOD"
    decision = decide(
        invoice_id="invoice-1",
        due_at=START,
        now=START + timedelta(days=7),
        attempt=2,
        policy=DunningPolicy(grace_days=7, suspend_days=21),
    )
    assert decision.state == "GRACE_PERIOD"
    assert decision.event_key == "dunning:invoice-1:GRACE_PERIOD:2"
