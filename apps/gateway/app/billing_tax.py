"""Deterministic tax/VAT evidence and immutable invoice snapshots."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
import json


CENT = Decimal("0.01")


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TaxSnapshot:
    jurisdiction: str
    mode: str
    rate: Decimal
    evidence_label: str
    customer_tax_id: str | None = None

    def as_json(self) -> str:
        payload = asdict(self)
        payload["rate"] = str(payload["rate"])
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def calculate_tax(subtotal: Decimal | int | str, snapshot: TaxSnapshot) -> Decimal:
    if snapshot.rate < 0 or snapshot.rate > 1:
        raise ValueError("tax_rate_out_of_range")
    if snapshot.mode == "NO_TAX":
        return Decimal("0.00")
    return money(money(subtotal) * snapshot.rate)


def invoice_tax_evidence(snapshot: TaxSnapshot, subtotal: Decimal | int | str) -> dict[str, str]:
    return {
        "jurisdiction": snapshot.jurisdiction,
        "mode": snapshot.mode,
        "rate": str(snapshot.rate),
        "evidence_label": snapshot.evidence_label,
        "subtotal": str(money(subtotal)),
        "tax": str(calculate_tax(subtotal, snapshot)),
        "snapshot": snapshot.as_json(),
    }
