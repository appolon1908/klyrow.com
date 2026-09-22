"Worker-owned reconciliation of durable provider evidence into billing facts."

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from .billing_ledger import post_settlement
from .billing_provider_events import BillingProviderEvent, mark_provider_event_failure, now, paypal_order_id
from .payment_attempts import AUTHORIZED, CAPTURED, CREATED, EXPIRED, FAILED, PENDING, REQUIRES_ACTION, PaymentAttempt, _apply_transition
from .paypal_provider import PayPalProviderError, get_paypal_provider


STRIPE_SUCCESS_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded", "payment_intent.succeeded"}
STRIPE_FAILURE_EVENTS = {"checkout.session.async_payment_failed", "payment_intent.payment_failed"}
PAYPAL_EVIDENCE_ONLY_EVENTS = {"PAYMENT.CAPTURE.REFUNDED", "CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.RESOLVED"}


def _failure(event: BillingProviderEvent, code: str) -> None:
    mark_provider_event_failure(event, code, retryable=False)


def process_stripe_provider_event(session, event: BillingProviderEvent) -> None:
    if event.processing_state != "PROCESSING":
        return
    if event.livemode:
        _failure(event, "stripe_livemode_rejected")
        return
    payload = json.loads(event.payload_json)
    obj = payload.get("data", {}).get("object", {})
    metadata = obj.get("metadata", {}) if isinstance(obj, dict) else {}
    if not isinstance(obj, dict) or not isinstance(metadata, dict):
        _failure(event, "stripe_payload_malformed")
        return
    if event.event_type in STRIPE_FAILURE_EVENTS:
        event.processing_state = "IGNORED"
        event.processed_at = now()
        return
    if event.event_type not in STRIPE_SUCCESS_EVENTS:
        event.processing_state = "IGNORED"
        event.processed_at = now()
        return
    attempt_id = metadata.get("payment_attempt_id") or event.payment_attempt_id
    invoice_id = metadata.get("invoice_id") or event.invoice_id
    tenant_id = metadata.get("tenant_id") or event.tenant_id
    if not all(isinstance(value, str) and value for value in (attempt_id, invoice_id, tenant_id)):
        _failure(event, "stripe_correlation_missing")
        return
    attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update())
    if not attempt or attempt.provider != "stripe" or attempt.tenant_id != tenant_id or attempt.invoice_id != invoice_id:
        _failure(event, "stripe_correlation_mismatch")
        return
    if event.event_type == "checkout.session.expired":
        if attempt.status in {CREATED, PENDING, REQUIRES_ACTION, AUTHORIZED}:
            _apply_transition(
                session,
                attempt,
                EXPIRED,
                event_type="billing.checkout.expired",
                source="provider_event",
                ctx={"sub": "stripe-webhook"},
                provider_event_reference=event.provider_event_id,
            )
        event.processing_state = "PROCESSED"
        event.processed_at = now()
        event.updated_at = now()
        return
    if event.event_type == "checkout.session.completed" and obj.get("payment_status") != "paid":
        if attempt.status == CREATED:
            _apply_transition(
                session,
                attempt,
                PENDING,
                event_type="billing.checkout.pending",
                source="provider_event",
                ctx={"sub": "stripe-webhook"},
                provider_event_reference=event.provider_event_id,
            )
        event.processing_state = "PROCESSED"
        event.processed_at = now()
        event.last_error_code = "stripe_checkout_not_paid"
        event.last_error_message = "stripe_checkout_not_paid"
        event.updated_at = now()
        return
    currency = str(obj.get("currency") or "").upper()
    amount_minor = obj.get("amount_total", obj.get("amount_received"))
    provider_reference = obj.get("payment_intent") or obj.get("id")
    if not isinstance(amount_minor, int) or not isinstance(provider_reference, str) or currency != attempt.currency:
        _failure(event, "stripe_amount_or_currency_mismatch")
        return
    if amount_minor != attempt.amount_minor:
        _failure(event, "stripe_amount_or_currency_mismatch")
        return
    if attempt.status == CAPTURED:
        event.processing_state = "PROCESSED"
        event.processed_at = now()
        return
    if attempt.status not in {PENDING, REQUIRES_ACTION, AUTHORIZED}:
        _failure(event, "stripe_attempt_not_active")
        return
    attempt.provider_attempt_reference = provider_reference
    payment = post_settlement(
        session,
        tenant_id=tenant_id,
        invoice_id=invoice_id,
        provider="stripe",
        provider_reference=provider_reference,
        amount=Decimal(amount_minor) / Decimal(100),
        currency=currency,
        confirmed_by="stripe-webhook",
        payment_attempt_id=attempt.id,
    )
    _apply_transition(
        session,
        attempt,
        CAPTURED,
        event_type="payment_attempt.provider_captured",
        source="provider_event",
        ctx={"sub": "stripe-webhook"},
        provider_event_reference=event.provider_event_id,
    )
    event.processing_state = "PROCESSED"
    event.processed_at = now()
    event.last_error_code = None
    event.last_error_message = None
    event.updated_at = now()
    return payment


def _paypal_attempt(session, event: BillingProviderEvent, payload: dict) -> tuple[PaymentAttempt | None, str | None]:
    order_id = paypal_order_id(payload)
    attempt = None
    if event.payment_attempt_id:
        attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.id == event.payment_attempt_id))
    if attempt is None and order_id:
        attempt = session.scalar(select(PaymentAttempt).where(
            PaymentAttempt.provider == "paypal",
            PaymentAttempt.provider_attempt_reference == order_id,
        ))
    return attempt, order_id


def _paypal_capture_amount(resource: dict) -> tuple[int | None, str, str | None]:
    amount = resource.get("amount", {}) if isinstance(resource, dict) else {}
    currency = str(amount.get("currency_code") or "").upper() if isinstance(amount, dict) else ""
    value = amount.get("value") if isinstance(amount, dict) else None
    capture_id = resource.get("id") if isinstance(resource, dict) else None
    try:
        amount_minor = int((Decimal(str(value)) * 100).to_integral_exact()) if value is not None else None
    except (InvalidOperation, ValueError):
        amount_minor = None
    return amount_minor, currency, capture_id if isinstance(capture_id, str) else None


def process_paypal_provider_event(session, event: BillingProviderEvent, *, capture_adapter=None) -> None:
    if event.processing_state != "PROCESSING":
        return
    payload = json.loads(event.payload_json)
    resource = payload.get("resource", {}) if isinstance(payload, dict) else {}
    if not isinstance(resource, dict):
        _failure(event, "paypal_payload_malformed")
        return
    attempt, order_id = _paypal_attempt(session, event, payload)
    if attempt is None or not order_id:
        _failure(event, "paypal_correlation_missing")
        return
    if attempt.provider != "paypal" or attempt.provider_attempt_reference != order_id:
        _failure(event, "paypal_correlation_mismatch")
        return
    if event.tenant_id and event.tenant_id != attempt.tenant_id:
        _failure(event, "paypal_correlation_mismatch")
        return
    if event.invoice_id and event.invoice_id != attempt.invoice_id:
        _failure(event, "paypal_correlation_mismatch")
        return

    if event.event_type == "CHECKOUT.ORDER.APPROVED":
        if attempt.status == CAPTURED:
            event.processing_state = "PROCESSED"
            event.processed_at = now()
            return
        if attempt.status not in {PENDING, REQUIRES_ACTION, AUTHORIZED}:
            _failure(event, "paypal_attempt_not_active")
            return
        if attempt.status != AUTHORIZED:
            _apply_transition(
                session,
                attempt,
                AUTHORIZED,
                event_type="billing.paypal.order_approved",
                source="provider_event",
                ctx={"sub": "paypal-webhook"},
                provider_event_reference=event.provider_event_id,
            )
            session.flush()
        try:
            adapter = capture_adapter or get_paypal_provider()
            adapter.capture_order(order_id=order_id, idempotency_key=f"klyrow-paypal-capture:{attempt.id}")
        except PayPalProviderError as exc:
            code = exc.args[0] if exc.args else "paypal_capture_unavailable"
            mark_provider_event_failure(
                event,
                code,
                retryable=code in {"paypal_capture_ambiguous", "paypal_capture_unavailable", "paypal_auth_ambiguous", "paypal_auth_unavailable"},
            )
            return
        event.processing_state = "PROCESSED"
        event.processed_at = now()
        event.last_error_code = None
        event.last_error_message = None
        event.updated_at = now()
        return

    if event.event_type == "PAYMENT.CAPTURE.COMPLETED":
        amount_minor, currency, capture_id = _paypal_capture_amount(resource)
        if amount_minor != attempt.amount_minor or currency != attempt.currency or not capture_id:
            _failure(event, "paypal_amount_or_currency_mismatch")
            return
        if attempt.status == CAPTURED:
            event.processing_state = "PROCESSED"
            event.processed_at = now()
            return
        if attempt.status not in {PENDING, REQUIRES_ACTION, AUTHORIZED}:
            _failure(event, "paypal_attempt_not_active")
            return
        payment = post_settlement(
            session,
            tenant_id=attempt.tenant_id,
            invoice_id=attempt.invoice_id,
            provider="paypal",
            provider_reference=capture_id,
            amount=Decimal(amount_minor) / Decimal(100),
            currency=currency,
            confirmed_by="paypal-webhook",
            payment_attempt_id=attempt.id,
        )
        _apply_transition(
            session,
            attempt,
            CAPTURED,
            event_type="payment_attempt.provider_captured",
            source="provider_event",
            ctx={"sub": "paypal-webhook"},
            provider_event_reference=event.provider_event_id,
        )
        event.processing_state = "PROCESSED"
        event.processed_at = now()
        event.last_error_code = None
        event.last_error_message = None
        event.updated_at = now()
        return payment

    if event.event_type == "PAYMENT.CAPTURE.DENIED":
        if attempt.status in {PENDING, REQUIRES_ACTION, AUTHORIZED}:
            _apply_transition(
                session,
                attempt,
                FAILED,
                event_type="payment_attempt.provider_failed",
                source="provider_event",
                ctx={"sub": "paypal-webhook"},
                provider_event_reference=event.provider_event_id,
                failure_code="paypal_capture_denied",
                failure_message="paypal_capture_denied",
            )
        event.processing_state = "PROCESSED"
        event.processed_at = now()
        event.updated_at = now()
        return

    if event.event_type in PAYPAL_EVIDENCE_ONLY_EVENTS:
        event.processing_state = "PROCESSED"
        event.processed_at = now()
        event.updated_at = now()
        return

    event.processing_state = "IGNORED"
    event.processed_at = now()
    event.updated_at = now()


def process_provider_event(session, event: BillingProviderEvent) -> None:
    if event.provider == "stripe":
        process_stripe_provider_event(session, event)
    elif event.provider == "paypal":
        process_paypal_provider_event(session, event)
    else:
        mark_provider_event_failure(event, "provider_unsupported", retryable=False)
