"""Durable Stripe webhook inbox; HTTP ingestion never settles an invoice."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .billing_config import BillingConfigError, _read_secret_file, load_billing_settings
from .main import Base, db
from .stripe_sandbox import StripeWebhookError, verify_stripe_signature

router = APIRouter(tags=["Billing provider webhooks"])
now = lambda: datetime.now(timezone.utc)
PROVIDER_EVENT_LEASE_SECONDS = 60
SUPPORTED = {"checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed", "checkout.session.expired", "payment_intent.succeeded", "payment_intent.payment_failed"}


class BillingProviderEvent(Base):
    __tablename__ = "klyrow_billing_provider_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    provider_event_id: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    tenant_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    payment_attempt_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    invoice_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    livemode: Mapped[bool] = mapped_column(default=False)
    api_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String)
    processing_state: Mapped[str] = mapped_column(String, default="RECEIVED", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("provider", "provider_event_id", name="uq_klyrow_billing_provider_event"),)


def _metadata(payload: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    obj = payload.get("data", {}).get("object", {})
    metadata = obj.get("metadata", {}) if isinstance(obj, dict) else {}
    return (metadata.get("tenant_id"), metadata.get("payment_attempt_id"), metadata.get("invoice_id")) if isinstance(metadata, dict) else (None, None, None)


@router.post("/v1/internal/billing/providers/stripe/webhook", status_code=202)
async def stripe_webhook(request: Request, stripe_signature: str = Header(default="", alias="Stripe-Signature"), session: Session = Depends(db)):
    try:
        settings = load_billing_settings()
        if not settings.enabled or not settings.webhook_processing_enabled or not settings.stripe.enabled or settings.stripe.environment not in {"sandbox", "production"}:
            raise HTTPException(503, "billing_webhook_disabled")
        if settings.stripe.environment == "production" and not settings.stripe.production_approved:
            raise HTTPException(503, "billing_webhook_not_approved")
        secret = _read_secret_file("KLYROW_STRIPE_WEBHOOK_SECRET_FILE", None)
    except BillingConfigError:
        raise HTTPException(503, "billing_webhook_disabled") from None
    body = await request.body()
    try:
        verify_stripe_signature(body, stripe_signature, secret)
        payload = json.loads(body)
        event_id, event_type = payload["id"], payload["type"]
        if not isinstance(event_id, str) or not isinstance(event_type, str): raise ValueError
    except (StripeWebhookError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(400, "stripe_webhook_invalid") from None
    tenant_id, attempt_id, invoice_id = _metadata(payload)
    event = BillingProviderEvent(id=str(uuid.uuid4()), provider="stripe", provider_event_id=event_id, event_type=event_type, tenant_id=tenant_id, payment_attempt_id=attempt_id, invoice_id=invoice_id, livemode=bool(payload.get("livemode")), api_version=payload.get("api_version"), payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")), payload_hash=hashlib.sha256(body).hexdigest(), processing_state="RECEIVED" if event_type in SUPPORTED else "IGNORED")
    try:
        session.add(event); session.commit()
    except IntegrityError:
        session.rollback()
        return {"accepted": True, "duplicate": True}
    return {"accepted": True, "duplicate": False}


def claim_provider_events(session: Session, worker_id: str, limit: int = 20) -> list[BillingProviderEvent]:
    recover_expired_provider_events(session)
    rows = session.scalars(select(BillingProviderEvent).where(BillingProviderEvent.processing_state.in_(("RECEIVED", "RETRY")), (BillingProviderEvent.next_retry_at.is_(None) | (BillingProviderEvent.next_retry_at <= now()))).order_by(BillingProviderEvent.received_at).with_for_update(skip_locked=True).limit(limit)).all()
    for event in rows:
        event.processing_state = "PROCESSING"; event.claimed_by = worker_id; event.claimed_at = now(); event.attempt_count += 1; event.updated_at = now()
    return rows


def recover_expired_provider_events(session: Session, max_attempts: int = 8) -> int:
    cutoff = now() - timedelta(seconds=PROVIDER_EVENT_LEASE_SECONDS)
    rows = session.scalars(select(BillingProviderEvent).where(
        BillingProviderEvent.processing_state == "PROCESSING",
        BillingProviderEvent.claimed_at.is_not(None),
        BillingProviderEvent.claimed_at <= cutoff,
    ).with_for_update(skip_locked=True)).all()
    for event in rows:
        event.processing_state = "DEAD_LETTER" if event.attempt_count >= max_attempts else "RETRY"
        event.next_retry_at = None if event.processing_state == "DEAD_LETTER" else now()
        event.last_error_code = "provider_event_claim_expired"
        event.last_error_message = "provider_event_claim_expired"
        event.claimed_at = None
        event.claimed_by = None
        event.updated_at = now()
    return len(rows)


def mark_provider_event_failure(event: BillingProviderEvent, code: str, *, retryable: bool) -> None:
    event.last_error_code = code; event.last_error_message = code; event.updated_at = now()
    if not retryable: event.processing_state = "DEAD_LETTER"; event.processed_at = now(); return
    event.processing_state = "DEAD_LETTER" if event.attempt_count >= 8 else "RETRY"
    event.next_retry_at = now() + timedelta(seconds=min(900, 2 ** event.attempt_count)) if event.processing_state == "RETRY" else None