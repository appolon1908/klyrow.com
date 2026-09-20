"""Canonical invoice balance and settlement authority.

Provider integrations supply evidence; this module owns durable payment
posting and the resulting invoice financial state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .billing import Invoice, Payment, Refund, money


@dataclass(frozen=True)
class InvoiceBalance:
    total: Decimal
    credits: Decimal
    settled: Decimal
    refunded: Decimal
    net_settled: Decimal
    remaining_due: Decimal
    financial_status: str


def invoice_balance(session: Session, invoice: Invoice) -> InvoiceBalance:
    settled = money(session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.invoice_id == invoice.id, Payment.status == "CONFIRMED",
    )) or 0)
    refunded = money(session.scalar(select(func.coalesce(func.sum(Refund.amount), 0)).where(
        Refund.payment_id.in_(select(Payment.id).where(Payment.invoice_id == invoice.id)),
        Refund.status == "CONFIRMED",
    )) or 0)
    total = money(invoice.total)
    credits = money(invoice.credits)
    gross_due = max(money(total - credits), Decimal("0.00"))
    net_settled = max(money(settled - refunded), Decimal("0.00"))
    remaining = max(money(gross_due - net_settled), Decimal("0.00"))
    if invoice.status in {"VOID", "CREDITED"}:
        status = invoice.status
    elif remaining == Decimal("0.00"):
        status = "PAID"
    elif net_settled > Decimal("0.00"):
        status = "PARTIALLY_PAID"
    else:
        status = "OPEN"
    return InvoiceBalance(total, credits, settled, refunded, net_settled, remaining, status)


def post_settlement(session: Session, *, tenant_id: str, invoice_id: str, provider: str, provider_reference: str, amount: Decimal, currency: str, confirmed_by: str | None, payment_attempt_id: str | None = None) -> Payment:
    """Post one confirmed settlement and derive the invoice state atomically.

    Caller owns the transaction and locks the invoice before calling this
    service when competing writers are possible.
    """
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant_id).with_for_update())
    if not invoice:
        raise ValueError("invoice_not_found")
    if invoice.currency != currency:
        raise ValueError("currency_mismatch")
    replay = session.scalar(select(Payment).where(Payment.provider == provider, Payment.provider_reference == provider_reference).with_for_update())
    if replay and replay.status == "CONFIRMED":
        if replay.invoice_id != invoice.id or replay.tenant_id != tenant_id or money(replay.amount) != money(amount) or replay.currency != currency:
            raise ValueError("provider_reference_conflict")
        return replay
    if replay and (replay.invoice_id != invoice.id or replay.tenant_id != tenant_id or money(replay.amount) != money(amount) or replay.currency != currency):
        raise ValueError("provider_reference_conflict")
    balance = invoice_balance(session, invoice)
    if invoice.status in {"VOID", "CREDITED", "PAID"} or balance.remaining_due <= Decimal("0.00"):
        raise ValueError("invoice_not_payable")
    if money(amount) <= Decimal("0.00") or money(amount) > balance.remaining_due:
        raise ValueError("amount_exceeds_remaining_due")
    payment = replay or Payment(
        id=__import__("uuid").uuid4().hex, tenant_id=tenant_id, invoice_id=invoice.id,
        provider=provider, provider_reference=provider_reference, amount=money(amount),
        currency=currency, status="PENDING_RECONCILIATION", confirmed_by=None,
    )
    payment.status = "CONFIRMED"
    payment.confirmed_by = confirmed_by
    if payment_attempt_id is not None:
        setattr(payment, "payment_attempt_id", payment_attempt_id)
    session.add(payment)
    session.flush()
    invoice.status = invoice_balance(session, invoice).financial_status
    return payment