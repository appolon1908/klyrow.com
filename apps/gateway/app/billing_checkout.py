"""Application service for sandbox Stripe hosted Checkout."""
from __future__ import annotations

import uuid
from decimal import Decimal
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .billing_config import BillingConfigError, _read_secret_file, load_billing_settings
from .stripe_sandbox import StripeCheckoutResult, StripeSandboxAdapter, StripeWebhookError

ACTIVE_CHECKOUT_STATES = frozenset({"CREATED", "PENDING", "REQUIRES_ACTION", "AUTHORIZED"})
ALLOWED_CHECKOUT_HOSTS = frozenset({"checkout.stripe.com", "checkout.stripe.test"})


def _validate_checkout_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_CHECKOUT_HOSTS or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(502, "stripe_checkout_invalid_response")
    return value


def get_checkout_provider() -> StripeSandboxAdapter:
    try:
        settings = load_billing_settings()
        if not settings.enabled or not settings.webhook_processing_enabled or not settings.stripe.enabled or settings.stripe.environment != "sandbox":
            raise HTTPException(503, "stripe_checkout_unavailable")
        secret = _read_secret_file("KLYROW_STRIPE_SECRET_FILE", None)
    except BillingConfigError:
        raise HTTPException(503, "stripe_checkout_unavailable") from None
    return StripeSandboxAdapter(secret)


def _success_url(invoice_id: str) -> str:
    from .auth_bff import _public_origin
    return f"{_public_origin()}/app/billing/invoices/{invoice_id}"


def create_or_resume_stripe_checkout(session: Session, *, tenant_id: str, invoice_id: str, actor: str, idempotency_key: str) -> dict:
    from .billing import Invoice
    from .billing_ledger import invoice_balance
    from .payment_attempts import CREATED, PENDING, REQUIRES_ACTION, PaymentAttempt, PaymentAttemptCreateIn, _apply_transition, _fingerprint, _serialize, enforce_billing_capability, now
    enforce_billing_capability("stripe")
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant_id).with_for_update())
    if not invoice:
        raise HTTPException(404, "invoice_not_found")
    balance = invoice_balance(session, invoice)
    if invoice.status in {"PAID", "VOID", "CREDITED"} or balance.remaining_due <= Decimal("0.00"):
        raise HTTPException(409, "invoice_not_payable")
    amount_minor = int(balance.remaining_due * 100)
    existing = session.scalar(select(PaymentAttempt).where(PaymentAttempt.tenant_id == tenant_id, PaymentAttempt.invoice_id == invoice_id, PaymentAttempt.idempotency_key == idempotency_key))
    if existing:
        if existing.provider != "stripe":
            raise HTTPException(409, "checkout_idempotency_conflict")
        if existing.status == REQUIRES_ACTION and existing.next_action_reference:
            return _serialize(existing) | {"checkout_url": existing.next_action_reference, "duplicate": True}
        attempt = existing
    else:
        active = session.scalar(select(PaymentAttempt).where(PaymentAttempt.tenant_id == tenant_id, PaymentAttempt.invoice_id == invoice_id, PaymentAttempt.provider == "stripe", PaymentAttempt.status.in_(ACTIVE_CHECKOUT_STATES)).with_for_update())
        if active:
            raise HTTPException(409, "checkout_already_in_progress")
        attempt = PaymentAttempt(id=str(uuid.uuid4()), tenant_id=tenant_id, invoice_id=invoice_id, provider="stripe", idempotency_key=idempotency_key, request_fingerprint=_fingerprint({"tenant": tenant_id}, PaymentAttemptCreateIn(invoice_id=invoice_id, amount_minor=amount_minor, currency=invoice.currency, provider="stripe")), amount_minor=amount_minor, currency=invoice.currency, status=CREATED, created_by=actor, expires_at=now())
        session.add(attempt)
        session.flush()
    session.commit()

    try:
        result: StripeCheckoutResult = get_checkout_provider().create_checkout(
            payment_attempt_id=attempt.id, invoice_id=invoice_id, tenant_id=tenant_id,
            amount_minor=amount_minor, currency=invoice.currency,
            idempotency_key=f"klyrow-checkout:{attempt.id}",
            success_url=_success_url(invoice_id), cancel_url=_success_url(invoice_id),
        )
        checkout_url = _validate_checkout_url(result.checkout_url)
    except StripeWebhookError:
        raise HTTPException(503, "stripe_checkout_unavailable") from None

    attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.id == attempt.id, PaymentAttempt.tenant_id == tenant_id).with_for_update())
    if not attempt:
        raise HTTPException(404, "payment_attempt_not_found")
    attempt.provider_attempt_reference = result.session_id
    _apply_transition(session, attempt, PENDING, event_type="payment_attempt.checkout_created", source="application", ctx={"sub": actor})
    _apply_transition(session, attempt, REQUIRES_ACTION, event_type="payment_attempt.checkout_requires_action", source="application", ctx={"sub": actor}, next_action_type="stripe_checkout", next_action_reference=checkout_url)
    session.commit()
    return _serialize(attempt) | {"checkout_url": checkout_url, "duplicate": existing is not None}
