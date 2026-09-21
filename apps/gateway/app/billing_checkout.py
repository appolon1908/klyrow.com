"""Application service for sandbox hosted billing checkout."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth_bff import _public_origin
from .billing import Invoice, money
from .billing_config import BillingConfigError, load_billing_settings
from .main import audit, scoped_idempotency_key, semantic_request_hash
from .payment_attempts import (
    CREATED,
    REQUIRES_ACTION,
    PaymentAttempt,
    PaymentAttemptEvent,
    _apply_transition,
    _minor_from_decimal,
)
from .stripe_sandbox import StripeWebhookError, get_checkout_provider

ACTIVE_CHECKOUT_STATES = frozenset({CREATED, "PENDING", REQUIRES_ACTION, "AUTHORIZED"})


@dataclass(frozen=True)
class CheckoutCommand:
    attempt_id: str
    invoice_id: str
    amount_minor: int
    currency: str
    status: str
    checkout_url: str | None
    expires_at: Any


def _settings():
    try:
        settings = load_billing_settings()
    except BillingConfigError:
        raise HTTPException(503, "billing_checkout_disabled") from None
    if (
        not settings.enabled
        or not settings.stripe.enabled
        or settings.stripe.environment not in {"sandbox", "production"}
        or settings.stripe.environment == "production" and (not settings.live_charging_enabled or not settings.stripe.production_approved)
        or not settings.webhook_processing_enabled
    ):
        raise HTTPException(503, "billing_checkout_disabled")
    return settings


def _checkout_urls(invoice_id: str) -> tuple[str, str]:
    origin = _public_origin()
    path = f"/app/billing/invoices/{invoice_id}"
    return origin + path + "?checkout=return", origin + path + "?checkout=cancelled"


def _result(attempt: PaymentAttempt) -> CheckoutCommand:
    return CheckoutCommand(
        attempt_id=attempt.id,
        invoice_id=attempt.invoice_id,
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        status=attempt.status,
        checkout_url=attempt.next_action_reference,
        expires_at=attempt.expires_at,
    )


def create_or_resume_stripe_checkout(
    session: Session,
    *,
    tenant_id: str,
    actor_id: str,
    invoice_id: str,
    idempotency_key: str,
) -> CheckoutCommand:
    """Reserve one checkout, call Stripe without a DB lock, then persist it."""
    settings = _settings()
    storage_key = scoped_idempotency_key(
        {"tenant": tenant_id, "sub": actor_id},
        idempotency_key,
        action="billing.checkout.create",
        resource="invoice_checkout",
    )
    fingerprint = semantic_request_hash(
        action="billing.checkout.create",
        resource="invoice_checkout",
        payload={"tenant_id": tenant_id, "invoice_id": invoice_id, "provider": "stripe"},
    )

    invoice = session.scalar(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant_id).with_for_update()
    )
    if invoice is None:
        raise HTTPException(404, "invoice_not_found")
    existing = session.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.tenant_id == tenant_id,
            PaymentAttempt.idempotency_key == storage_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        if existing.status in {"FAILED", "CANCELLED", "EXPIRED"}:
            existing = None
        elif existing.status == REQUIRES_ACTION and existing.next_action_reference:
            return _result(existing)
        else:
            attempt = existing
    else:
        attempt = None

    if existing is None:
        active = session.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.tenant_id == tenant_id,
                PaymentAttempt.invoice_id == invoice_id,
                PaymentAttempt.provider == "stripe",
                PaymentAttempt.status.in_(ACTIVE_CHECKOUT_STATES),
            )
        )
        if active is not None:
            raise HTTPException(409, "checkout_already_in_progress")
        from .billing_ledger import invoice_balance

        balance = invoice_balance(session, invoice)
        if invoice.status in {"VOID", "CREDITED"} or balance.remaining_due <= 0:
            raise HTTPException(409, "invoice_not_payable")
        attempt = PaymentAttempt(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            invoice_id=invoice.id,
            customer_id=actor_id,
            provider="stripe",
            idempotency_key=storage_key,
            request_fingerprint=fingerprint,
            amount_minor=_minor_from_decimal(balance.remaining_due),
            currency=invoice.currency,
            status=CREATED,
            created_by=actor_id,
        )
        session.add(attempt)
        session.flush()
        _apply_transition(
            session,
            attempt,
            "PENDING",
            event_type="billing.checkout.started",
            source="browser_bff",
            ctx={"sub": actor_id},
            idempotency_key=storage_key,
        )
        audit(session, {"tenant": tenant_id, "sub": actor_id}, "billing.checkout.started")
        session.commit()
    else:
        session.commit()

    adapter = get_checkout_provider("stripe", settings=settings)
    success_url, cancel_url = _checkout_urls(invoice_id)
    try:
        provider_result = adapter.create_checkout(
            payment_attempt_id=attempt.id,
            invoice_id=invoice_id,
            tenant_id=tenant_id,
            amount_minor=attempt.amount_minor,
            currency=attempt.currency,
            idempotency_key=f"klyrow-checkout:{attempt.id}",
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except StripeWebhookError as exc:
        raise HTTPException(503, exc.args[0]) from None

    locked_attempt = session.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.id == attempt.id,
            PaymentAttempt.tenant_id == tenant_id,
        ).with_for_update()
    )
    if locked_attempt is None:
        raise HTTPException(404, "payment_attempt_not_found")
    if locked_attempt.status in {"FAILED", "CANCELLED", "EXPIRED", "CAPTURED"}:
        return _result(locked_attempt)
    locked_attempt.provider_attempt_reference = provider_result.session_id
    _apply_transition(
        session,
        locked_attempt,
        REQUIRES_ACTION,
        event_type="billing.checkout.provider_created",
        source="stripe",
        ctx={"sub": actor_id},
        next_action_type="hosted_checkout",
        next_action_reference=provider_result.checkout_url,
    )
    audit(session, {"tenant": tenant_id, "sub": actor_id}, "billing.checkout.provider_created")
    session.commit()
    return _result(locked_attempt)
