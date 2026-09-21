"""Reproducible invoice, credit-note, and receipt document authority."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json

from .billing_tax import money


@dataclass(frozen=True)
class DocumentLine:
    description: str
    quantity: int
    amount: Decimal


@dataclass(frozen=True)
class BillingDocument:
    document_type: str
    document_number: str
    tenant_id: str
    currency: str
    lines: tuple[DocumentLine, ...]
    total: Decimal
    source_reference: str
    tax_evidence: str | None = None

    def canonical_payload(self) -> str:
        payload = {
            "document_type": self.document_type,
            "document_number": self.document_number,
            "tenant_id": self.tenant_id,
            "currency": self.currency,
            "lines": [
                {"description": line.description, "quantity": line.quantity, "amount": str(money(line.amount))}
                for line in self.lines
            ],
            "total": str(money(self.total)),
            "source_reference": self.source_reference,
            "tax_evidence": self.tax_evidence,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @property
    def content_hash(self) -> str:
        return sha256(self.canonical_payload().encode("utf-8")).hexdigest()


def invoice_document(*, tenant_id: str, number: str, currency: str, lines: tuple[DocumentLine, ...], total: Decimal, tax_evidence: str | None = None) -> BillingDocument:
    return BillingDocument("INVOICE", number, tenant_id, currency, lines, money(total), number, tax_evidence)


def credit_note_document(*, tenant_id: str, number: str, invoice_number: str, currency: str, amount: Decimal, reason: str) -> BillingDocument:
    return BillingDocument("CREDIT_NOTE", number, tenant_id, currency, (DocumentLine(reason, 1, money(amount)),), money(amount), invoice_number)


def receipt_document(*, tenant_id: str, payment_reference: str, currency: str, amount: Decimal, invoice_number: str) -> BillingDocument:
    return BillingDocument("RECEIPT", payment_reference, tenant_id, currency, (DocumentLine("Confirmed payment", 1, money(amount)),), money(amount), invoice_number)


def require_confirmed_payment(status: str) -> None:
    if status != "CONFIRMED":
        raise ValueError("receipt_requires_confirmed_payment")
