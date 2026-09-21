"""Read-only billing reconciliation authority for M6D.

The report describes inconsistencies between provider evidence, payment-attempt
lifecycle, the canonical payment ledger, and invoice balances. It never mutates
billing history.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .billing import Invoice, Payment, Refund
from .billing_ledger import invoice_balance
from .billing_provider_events import BillingProviderEvent
from .main import db, require
from .payment_attempts import CAPTURED, PaymentAttempt

router = APIRouter(prefix="/v1/internal/billing", tags=["Billing reconciliation"])


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    tenant_id: str
    resource_id: str | None
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "tenant_id": self.tenant_id,
            "resource_id": self.resource_id,
            "details": self.details,
        }


def _issue(code: str, tenant_id: str, resource_id: str | None, **details: Any) -> ReconciliationIssue:
    return ReconciliationIssue(code, tenant_id, resource_id, details)


def reconcile_billing(session: Session, *, tenant_id: str | None = None) -> list[ReconciliationIssue]:
    """Return deterministic billing drift findings without changing rows."""
    issues: list[ReconciliationIssue] = []
    attempt_query = select(PaymentAttempt)
    payment_query = select(Payment)
    invoice_query = select(Invoice)
    event_query = select(BillingProviderEvent)
    if tenant_id is not None:
        attempt_query = attempt_query.where(PaymentAttempt.tenant_id == tenant_id)
        payment_query = payment_query.where(Payment.tenant_id == tenant_id)
        invoice_query = invoice_query.where(Invoice.tenant_id == tenant_id)
        event_query = event_query.where(BillingProviderEvent.tenant_id == tenant_id)

    attempts = session.scalars(attempt_query).all()
    payments = session.scalars(payment_query).all()
    attempts_by_id = {item.id: item for item in attempts}
    invoices_by_id = {item.id: item for item in session.scalars(invoice_query).all()}
    payments_by_attempt = {item.payment_attempt_id: item for item in payments if item.payment_attempt_id}

    for attempt in attempts:
        payment = payments_by_attempt.get(attempt.id)
        if attempt.status == CAPTURED and payment is None:
            issues.append(_issue("captured_attempt_missing_payment", attempt.tenant_id, attempt.id))
        if payment is not None:
            if payment.tenant_id != attempt.tenant_id:
                issues.append(_issue("payment_attempt_tenant_mismatch", attempt.tenant_id, attempt.id, payment_id=payment.id))
            if payment.invoice_id != attempt.invoice_id:
                issues.append(_issue("payment_attempt_invoice_mismatch", attempt.tenant_id, attempt.id, payment_id=payment.id))
            if payment.currency != attempt.currency:
                issues.append(_issue("payment_attempt_currency_mismatch", attempt.tenant_id, attempt.id, payment_id=payment.id))
            expected_amount = Decimal(attempt.amount_minor) / Decimal(100)
            if Decimal(payment.amount) != expected_amount:
                issues.append(_issue("payment_attempt_amount_mismatch", attempt.tenant_id, attempt.id, payment_id=payment.id))
            if payment.status == "CONFIRMED" and attempt.status != CAPTURED:
                issues.append(_issue("confirmed_payment_attempt_not_captured", attempt.tenant_id, attempt.id, payment_id=payment.id))

    for event in session.scalars(event_query).all():
        attempt = attempts_by_id.get(event.payment_attempt_id) if event.payment_attempt_id else None
        if event.payment_attempt_id and attempt is None:
            issues.append(_issue("provider_event_attempt_missing", event.tenant_id or "", event.id))
        if attempt is not None:
            if event.tenant_id != attempt.tenant_id:
                issues.append(_issue("provider_event_tenant_mismatch", event.tenant_id or "", event.id, attempt_id=attempt.id))
            if event.invoice_id and event.invoice_id != attempt.invoice_id:
                issues.append(_issue("provider_event_invoice_mismatch", event.tenant_id or "", event.id, attempt_id=attempt.id))
            try:
                payload = json.loads(event.payload_json or "{}")
            except (TypeError, ValueError):
                payload = {}
            obj = payload.get("data", {}).get("object", {}) if isinstance(payload, dict) else {}
            if isinstance(obj, dict):
                provider_currency = str(obj.get("currency") or "").upper()
                provider_amount = obj.get("amount_total", obj.get("amount_received"))
                if provider_currency and provider_currency != attempt.currency:
                    issues.append(_issue("provider_event_currency_mismatch", event.tenant_id or "", event.id, attempt_id=attempt.id))
                if isinstance(provider_amount, int) and provider_amount != attempt.amount_minor:
                    issues.append(_issue("provider_event_amount_mismatch", event.tenant_id or "", event.id, attempt_id=attempt.id))
        claimed_at = event.claimed_at
        if claimed_at is not None and claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        if (
            event.processing_state == "PROCESSING"
            and claimed_at is not None
            and claimed_at < datetime.now(timezone.utc) - timedelta(minutes=5)
        ):
            issues.append(_issue("provider_event_stale_processing_lease", event.tenant_id or "", event.id))

    for invoice in session.scalars(invoice_query).all():
        balance = invoice_balance(session, invoice)
        if invoice.status == "PAID" and balance.remaining_due > 0:
            issues.append(_issue("invoice_paid_with_remaining_due", invoice.tenant_id, invoice.id))
        if invoice.status == "OPEN" and balance.remaining_due == 0:
            issues.append(_issue("invoice_open_with_zero_remaining_due", invoice.tenant_id, invoice.id))
        payment_ids = select(Payment.id).where(Payment.invoice_id == invoice.id)
        refunded = session.scalar(select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.payment_id.in_(payment_ids), Refund.status == "CONFIRMED"
        )) or 0
        paid = session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.invoice_id == invoice.id, Payment.status == "CONFIRMED"
        )) or 0
        if Decimal(refunded) > Decimal(paid):
            issues.append(_issue("refund_exceeds_payment", invoice.tenant_id, invoice.id))

    for payment in payments:
        if payment.invoice_id is None or payment.invoice_id not in invoices_by_id:
            issues.append(_issue("confirmed_payment_without_invoice", payment.tenant_id, payment.id))
    duplicate_attempts = session.execute(select(Payment.payment_attempt_id, func.count(Payment.id)).where(
        Payment.payment_attempt_id.is_not(None)
    ).group_by(Payment.payment_attempt_id).having(func.count(Payment.id) > 1)).all()
    for attempt_id, count in duplicate_attempts:
        attempt = session.get(PaymentAttempt, attempt_id)
        issues.append(_issue("duplicate_payment_attempt_payment", attempt.tenant_id if attempt else "", attempt_id, count=count))
    return issues


@router.get("/reconciliation")
def reconciliation_report(
    tenant_id: str | None = Query(default=None, min_length=1, max_length=200),
    ctx=Depends(require("platform_admin")),
    session: Session = Depends(db),
) -> dict[str, Any]:
    """Return an operator-only, read-only reconciliation report."""
    report = reconcile_billing(session, tenant_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "status": "PASS" if not report else "DRIFT",
        "issue_count": len(report),
        "issues": [issue.as_dict() for issue in report],
    }
