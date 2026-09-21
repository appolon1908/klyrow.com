"""Provider-neutral refund and dispute authority for the M6C billing lane."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from fastapi import HTTPException


REFUND_STATES = {"REQUESTED", "PROVIDER_PENDING", "CONFIRMED", "FAILED", "REVERSED"}
REFUND_TRANSITIONS = {
    "REQUESTED": {"PROVIDER_PENDING", "FAILED"},
    "PROVIDER_PENDING": {"CONFIRMED", "FAILED"},
    "CONFIRMED": {"REVERSED"},
    "FAILED": set(),
    "REVERSED": set(),
}
DISPUTE_STATES = {"OPEN", "EVIDENCE_REQUIRED", "SUBMITTED", "WON", "LOST", "CLOSED"}


def money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def confirmed_refund_total(refunds: Iterable[object]) -> Decimal:
    return money(
        sum(
            (
                money(getattr(item, "amount", 0))
                for item in refunds
                if getattr(item, "status", "CONFIRMED") == "CONFIRMED"
            ),
            Decimal("0"),
        )
    )


def refundable_amount(payment_amount: Decimal | int | str, refunds: Iterable[object]) -> Decimal:
    return max(money(payment_amount) - confirmed_refund_total(refunds), Decimal("0.00"))


def validate_refund_amount(
    payment_amount: Decimal | int | str,
    refunds: Iterable[object],
    requested_amount: Decimal | int | str,
) -> Decimal:
    amount = money(requested_amount)
    if amount <= 0:
        raise HTTPException(422, "refund_amount_must_be_positive")
    available = refundable_amount(payment_amount, refunds)
    if amount > available:
        raise HTTPException(409, "refund_exceeds_refundable_amount")
    return amount


def validate_refund_transition(current: str, target: str) -> None:
    if current not in REFUND_STATES or target not in REFUND_STATES:
        raise HTTPException(422, "invalid_refund_state")
    if target not in REFUND_TRANSITIONS[current]:
        raise HTTPException(409, "invalid_refund_transition")


def validate_dispute_state(state: str) -> None:
    if state not in DISPUTE_STATES:
        raise HTTPException(422, "invalid_dispute_state")