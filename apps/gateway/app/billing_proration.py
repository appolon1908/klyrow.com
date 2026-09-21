"""Deterministic provider-neutral subscription change quotes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ProrationQuote:
    old_price: Decimal
    new_price: Decimal
    fraction_remaining: Decimal
    charge: Decimal
    credit: Decimal
    effective: str


def quote_plan_change(
    *,
    old_price: Decimal,
    new_price: Decimal,
    period_start: datetime,
    period_end: datetime,
    at: datetime,
) -> ProrationQuote:
    total_seconds = Decimal(str(max(1, (period_end - period_start).total_seconds())))
    remaining_seconds = Decimal(str(max(0, (period_end - at).total_seconds())))
    fraction = min(Decimal("1"), remaining_seconds / total_seconds)
    delta = money((money(new_price) - money(old_price)) * fraction)
    return ProrationQuote(
        old_price=money(old_price),
        new_price=money(new_price),
        fraction_remaining=fraction,
        charge=max(Decimal("0.00"), delta),
        credit=max(Decimal("0.00"), -delta),
        effective="IMMEDIATE" if delta >= 0 else "NEXT_PERIOD",
    )
