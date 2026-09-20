"""Worker-owned reconciliation of durable Stripe evidence into billing facts."""
from __future__ import annotations

from decimal import Decimal

from .billing_ledger import post_settlement
from .billing_provider_events import BillingProviderEvent, mark_provider_event_failure, now
from .payment_attempts import CAPTURED, PENDING, PaymentAttempt, _apply_transition


SUCCESS_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded", "payment_intent.succeeded"}
FAILURE_EVENTS = {"checkout.session.async_payment_failed", "payment_intent.payment_failed"}


def _failure(event: BillingProviderEvent, code: str) -> None:
    mark_provider_event_failure(event, code, retryable=False)


def process_stripe_provider_event(session, event: BillingProviderEvent) -> None:
    """Process one claimed inbox row without committing the caller transaction."""
    if event.processing_state != "PROCESSING":
        return
    if event.livemode:
        _failure(event, "stripe_livemode_rejected")
        return
    payload = __import__("json").loads(event.payload_json)
    obj = payload.get("data", {}).get("object", {})
    metadata = obj.get("metadata", {}) if isinstance(obj, dict) else {}
    if not isinstance(obj, dict) or not isinstance(metadata, dict):
        _failure(event, "stripe_payload_malformed")
        return
    if event.event_type in FAILURE_EVENTS:
        event.processing_state = "IGNORED"; event.processed_at = now(); return
    if event.event_type not in SUCCESS_EVENTS:
        event.processing_state = "IGNORED"; event.processed_at = now(); return
    attempt_id = metadata.get("payment_attempt_id") or event.payment_attempt_id
    invoice_id = metadata.get("invoice_id") or event.invoice_id
    tenant_id = metadata.get("tenant_id") or event.tenant_id
    if not all(isinstance(value, str) and value for value in (attempt_id, invoice_id, tenant_id)):
        _failure(event, "stripe_correlation_missing")
        return
    attempt = session.scalar(__import__("sqlalchemy").select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update())
    if not attempt or attempt.provider != "stripe" or attempt.tenant_id != tenant_id or attempt.invoice_id != invoice_id:
        _failure(event, "stripe_correlation_mismatch")
        return
    currency = str(obj.get("currency") or "").upper()
    amount_minor = obj.get("amount_total", obj.get("amount_received"))
    provider_reference = obj.get("payment_intent") or obj.get("id")
    if not isinstance(amount_minor, int) or not isinstance(provider_reference, str) or currency != attempt.currency:
        _failure(event, "stripe_amount_or_currency_mismatch")
        return
    expected_minor = attempt.amount_minor
    if amount_minor != expected_minor:
        _failure(event, "stripe_amount_or_currency_mismatch")
        return
    if attempt.status == CAPTURED:
        event.processing_state = "PROCESSED"; event.processed_at = now(); return
    if attempt.status != PENDING:
        _failure(event, "stripe_attempt_not_pending")
        return
    attempt.provider_attempt_reference = provider_reference
    payment = post_settlement(
        session, tenant_id=tenant_id, invoice_id=invoice_id, provider="stripe",
        provider_reference=provider_reference, amount=Decimal(amount_minor) / Decimal(100),
        currency=currency, confirmed_by="stripe-webhook", payment_attempt_id=attempt.id,
    )
    _apply_transition(
        session, attempt, CAPTURED, event_type="payment_attempt.provider_captured",
        source="provider_event", ctx={"sub": "stripe-webhook"},
        provider_event_reference=event.provider_event_id,
    )
    event.processing_state = "PROCESSED"; event.processed_at = now(); event.last_error_code = None; event.last_error_message = None
    event.updated_at = now()
    return payment