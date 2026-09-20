"""Mission 02: provider-neutral PaymentAttempt foundation.

Extends the existing Klyrow billing core (`billing.py`) and the Mission 01
billing configuration authority (`billing_config.py`). No PaymentAttempt path
in this module makes a provider network call, stores a raw credential, moves
money, or activates a live provider. Every provider selection other than the
explicit `"disabled"` sentinel fails closed, because no real adapter is
registered in this mission.

Amount conversion boundary: existing `Invoice`/`Payment`/`Credit` rows use
`Decimal` amounts quantized to 2 places (see `billing.money`). PaymentAttempt
amounts are integer minor units (e.g. cents) for the currencies this
repository currently supports, all of which use 2 minor-unit decimal places.
`_minor_from_decimal`/`_decimal_from_minor` are the only conversion points;
multi-decimal-currency support (e.g. 0- or 3-decimal ISO currencies) is an
explicit Mission 03+ deferral.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from prometheus_client import Counter
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .billing import Invoice, Payment, PaymentMethodReference, money
from .billing_config import BillingConfigError, load_billing_settings
from .main import (
    Base,
    audit,
    auth,
    db,
    platform_metric,
    require,
    scoped_idempotency_key,
    semantic_request_hash,
)

router = APIRouter(prefix="/v1", tags=["Klyrow payment attempts"])

now = lambda: datetime.now(timezone.utc)

# --------------------------------------------------------------------------
# Amount conversion boundary
# --------------------------------------------------------------------------

TWO_DECIMAL_CURRENCIES = frozenset({
    "AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "HKD", "NZD", "SGD", "USD",
})


def _minor_from_decimal(amount: Decimal) -> int:
    return int((money(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _decimal_from_minor(amount_minor: int) -> Decimal:
    return money(Decimal(amount_minor) / Decimal(100))


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------

CREATED = "CREATED"
PENDING = "PENDING"
REQUIRES_ACTION = "REQUIRES_ACTION"
AUTHORIZED = "AUTHORIZED"
CAPTURED = "CAPTURED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"

STATES = frozenset({CREATED, PENDING, REQUIRES_ACTION, AUTHORIZED, CAPTURED, FAILED, CANCELLED, EXPIRED})
TERMINAL_STATES = frozenset({CAPTURED, FAILED, CANCELLED, EXPIRED})

TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({PENDING, CANCELLED, EXPIRED}),
    PENDING: frozenset({REQUIRES_ACTION, AUTHORIZED, CAPTURED, FAILED, CANCELLED, EXPIRED}),
    REQUIRES_ACTION: frozenset({PENDING, AUTHORIZED, CAPTURED, FAILED, CANCELLED, EXPIRED}),
    AUTHORIZED: frozenset({CAPTURED, FAILED, CANCELLED, EXPIRED}),
    CAPTURED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    EXPIRED: frozenset(),
}

_TIMESTAMP_FIELD = {
    AUTHORIZED: "authorized_at",
    CAPTURED: "captured_at",
    FAILED: "failed_at",
    CANCELLED: "cancelled_at",
}


class PaymentAttemptConflict(HTTPException):
    def __init__(self, code: str):
        super().__init__(status_code=409, detail=code)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

class PaymentAttempt(Base):
    __tablename__ = "klyrow_payment_attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    invoice_id: Mapped[str] = mapped_column(String, index=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payment_method_reference_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String)
    provider_account_reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    provider_attempt_reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_fingerprint: Mapped[str] = mapped_column(String)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=CREATED)
    failure_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    failure_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    next_action_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    next_action_reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    authorized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_klyrow_payment_attempts_tenant_idempotency"),
    )


class PaymentAttemptEvent(Base):
    __tablename__ = "klyrow_payment_attempt_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    payment_attempt_id: Mapped[str] = mapped_column(ForeignKey("klyrow_payment_attempts.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    to_status: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    provider_account_reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    provider_event_reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payload_digest: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_account_reference", "provider_event_reference",
            name="uq_klyrow_payment_attempt_events_provider_event",
        ),
    )


# --------------------------------------------------------------------------
# Provider-neutral adapter contract (no SDK, no network calls)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderAttemptCommand:
    payment_attempt_id: str
    provider: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class ProviderAttemptResult:
    status: str
    provider_attempt_reference: Optional[str] = None
    next_action_type: Optional[str] = None
    next_action_reference: Optional[str] = None
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None


class PaymentProviderAdapter(Protocol):
    def create_attempt(self, command: ProviderAttemptCommand) -> ProviderAttemptResult: ...
    def confirm_attempt(self, command: ProviderAttemptCommand) -> ProviderAttemptResult: ...
    def capture_attempt(self, command: ProviderAttemptCommand) -> ProviderAttemptResult: ...
    def cancel_attempt(self, command: ProviderAttemptCommand) -> ProviderAttemptResult: ...


class ProviderUnavailableError(RuntimeError):
    """Raised by an adapter operation this mission never actually executes."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DisabledProviderAdapter:
    """Fails closed for every operation; no network call, no state change."""

    def create_attempt(self, command: ProviderAttemptCommand) -> ProviderAttemptResult:
        raise ProviderUnavailableError("provider_unavailable")

    def confirm_attempt(self, command: ProviderAttemptCommand) -> ProviderAttemptResult:
        raise ProviderUnavailableError("provider_unavailable")

    def capture_attempt(self, command: ProviderAttemptCommand) -> ProviderAttemptResult:
        raise ProviderUnavailableError("provider_unavailable")

    def cancel_attempt(self, command: ProviderAttemptCommand) -> ProviderAttemptResult:
        raise ProviderUnavailableError("provider_unavailable")


SUPPORTED_PROVIDERS = frozenset({"disabled", "stripe", "paypal", "stablecoin"})
_DISABLED_ADAPTER = DisabledProviderAdapter()


def select_adapter(provider: str) -> PaymentProviderAdapter:
    """Mission 02 registers no real adapter; every provider resolves to disabled."""

    if provider not in SUPPORTED_PROVIDERS:
        raise ProviderUnavailableError("unsupported_provider")
    return _DISABLED_ADAPTER


def enforce_billing_capability(provider: str, *, require_live_charging: bool = False) -> None:
    """Fail closed per the canonical Mission 01 configuration authority.

    Never contacts a provider; only reads already-validated configuration.
    """

    try:
        settings = load_billing_settings()
    except BillingConfigError:
        raise HTTPException(503, "billing_disabled") from None
    if not settings.enabled:
        raise HTTPException(503, "billing_disabled")
    if provider != "disabled":
        provider_settings = getattr(settings, provider, None)
        if provider_settings is None or not provider_settings.enabled:
            raise HTTPException(503, "provider_disabled")
    if require_live_charging:
        if provider == "disabled":
            raise HTTPException(503, "provider_disabled")
        if not settings.live_charging_enabled:
            raise HTTPException(503, "live_charging_disabled")


# --------------------------------------------------------------------------
# Metrics (no high-cardinality dimensions: no attempt/invoice/tenant/reference ID)
# --------------------------------------------------------------------------

CREATED_TOTAL = platform_metric(Counter("billing_payment_attempt_created_total", "Payment attempts created", ["codestra_business", "application", "service", "environment", "server", "region", "deployment", "provider"]))
TRANSITION_TOTAL = platform_metric(Counter("billing_payment_attempt_transition_total", "Payment attempt transitions", ["codestra_business", "application", "service", "environment", "server", "region", "deployment", "transition"]))
FAILED_TOTAL = platform_metric(Counter("billing_payment_attempt_failed_total", "Payment attempts failed", ["codestra_business", "application", "service", "environment", "server", "region", "deployment", "failure_class"]))
IDEMPOTENT_REPLAY_TOTAL = platform_metric(Counter("billing_payment_attempt_idempotent_replay_total", "Idempotent payment attempt replays", ["codestra_business", "application", "service", "environment", "server", "region", "deployment", "provider"]))
CONFLICT_TOTAL = platform_metric(Counter("billing_payment_attempt_conflict_total", "Payment attempt idempotency/transition conflicts", ["codestra_business", "application", "service", "environment", "server", "region", "deployment", "provider"]))


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class PaymentAttemptCreateIn(BaseModel):
    invoice_id: str = Field(min_length=1, max_length=200)
    amount_minor: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    provider: str = Field(pattern="^(disabled|stripe|paypal|stablecoin)$")
    payment_method_reference_id: Optional[str] = Field(default=None, max_length=300)


class PaymentAttemptTransitionIn(BaseModel):
    target_status: str = Field(pattern="^(PENDING|REQUIRES_ACTION|AUTHORIZED|CAPTURED|FAILED|CANCELLED|EXPIRED)$")
    provider_event_reference: Optional[str] = Field(default=None, max_length=300)
    failure_code: Optional[str] = Field(default=None, max_length=100)
    failure_message: Optional[str] = Field(default=None, max_length=500)
    next_action_type: Optional[str] = Field(default=None, max_length=100)
    next_action_reference: Optional[str] = Field(default=None, max_length=300)


def _serialize(attempt: PaymentAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "invoice_id": attempt.invoice_id,
        "amount_minor": attempt.amount_minor,
        "currency": attempt.currency,
        "status": attempt.status,
        "provider": attempt.provider,
        "provider_attempt_reference": attempt.provider_attempt_reference,
        "next_action_type": attempt.next_action_type,
        "next_action_reference": attempt.next_action_reference,
        "correlation_id": attempt.correlation_id,
        "created_at": attempt.created_at,
        "updated_at": attempt.updated_at,
        "authorized_at": attempt.authorized_at,
        "captured_at": attempt.captured_at,
        "failed_at": attempt.failed_at,
        "cancelled_at": attempt.cancelled_at,
        "version": attempt.version,
    }


def _serialize_event(event: PaymentAttemptEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "event_type": event.event_type,
        "source": event.source,
        "correlation_id": event.correlation_id,
        "created_at": event.created_at,
    }


def _tenant_invoice(session: Session, invoice_id: str, tenant_id: str, *, for_update: bool = False) -> Invoice:
    query = select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant_id)
    if for_update:
        query = query.with_for_update()
    invoice = session.scalar(query)
    if not invoice:
        raise HTTPException(404, "invoice_not_found")
    return invoice


def _remaining_eligible_minor(session: Session, invoice: Invoice) -> int:
    """Remaining balance in minor units after confirmed legacy payments, credits,
    and already-captured PaymentAttempts. Refunds and wallet transactions are not
    payment attempts and do not participate in this calculation."""

    from sqlalchemy import func
    confirmed_payments = money(
        session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.invoice_id == invoice.id, Payment.status == "CONFIRMED"
        ))
    )
    captured_minor = session.scalar(
        select(func.coalesce(func.sum(PaymentAttempt.amount_minor), 0)).where(
            PaymentAttempt.invoice_id == invoice.id, PaymentAttempt.status == CAPTURED
        )
    ) or 0
    remaining = money(invoice.total) - money(invoice.credits) - confirmed_payments - _decimal_from_minor(captured_minor)
    return max(0, _minor_from_decimal(remaining))


def _validate_invoice_eligibility(session: Session, invoice: Invoice, tenant_id: str, amount_minor: int, currency: str) -> None:
    if invoice.status in {"PAID", "VOID", "CREDITED"}:
        raise HTTPException(409, "invoice_not_payable")
    if invoice.currency != currency:
        raise HTTPException(422, "currency_mismatch")
    if currency not in TWO_DECIMAL_CURRENCIES:
        raise HTTPException(422, "unsupported_currency_minor_units")
    remaining = _remaining_eligible_minor(session, invoice)
    if remaining <= 0:
        raise HTTPException(409, "invoice_not_payable")
    if amount_minor > remaining:
        raise HTTPException(409, "amount_exceeds_remaining_balance")


def _fingerprint(ctx: dict, x: PaymentAttemptCreateIn) -> str:
    return semantic_request_hash(
        action="payment_attempt.create",
        resource="payment_attempts",
        payload={
            "tenant_id": ctx["tenant"],
            "invoice_id": x.invoice_id,
            "payment_method_reference_id": x.payment_method_reference_id,
            "provider": x.provider,
            "amount_minor": x.amount_minor,
            "currency": x.currency,
        },
    )


@router.post("/billing/payment-attempts", status_code=201)
def create_payment_attempt(
    x: PaymentAttemptCreateIn,
    ctx=Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    x_correlation_id: Optional[str] = Header(default=None, alias="X-Correlation-Id", max_length=200),
):
    enforce_billing_capability(x.provider)
    fingerprint = _fingerprint(ctx, x)
    storage_key = scoped_idempotency_key(ctx, idempotency_key, action="payment_attempt.create", resource="payment_attempts")

    existing = s.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.tenant_id == ctx["tenant"], PaymentAttempt.idempotency_key == storage_key
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            CONFLICT_TOTAL.labels(x.provider).inc()
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        IDEMPOTENT_REPLAY_TOTAL.labels(x.provider).inc()
        return _serialize(existing)

    # Lock the invoice row so concurrent creation requests against the same
    # invoice serialize their remaining-balance check (PostgreSQL; SQLite
    # ignores FOR UPDATE, matching this repository's existing convention).
    invoice = _tenant_invoice(s, x.invoice_id, ctx["tenant"], for_update=True)
    _validate_invoice_eligibility(s, invoice, ctx["tenant"], x.amount_minor, x.currency)
    if x.payment_method_reference_id:
        method = s.scalar(
            select(PaymentMethodReference).where(
                PaymentMethodReference.id == x.payment_method_reference_id,
                PaymentMethodReference.tenant_id == ctx["tenant"],
                PaymentMethodReference.revoked_at.is_(None),
            )
        )
        if not method or x.provider == "disabled" or method.provider != "EXTERNAL_TOKENIZED":
            raise HTTPException(422, "invalid_payment_method_reference")
    select_adapter(x.provider)  # fail closed early for unsupported providers

    attempt = PaymentAttempt(
        id=str(uuid.uuid4()),
        tenant_id=ctx["tenant"],
        invoice_id=invoice.id,
        customer_id=ctx.get("sub"),
        payment_method_reference_id=x.payment_method_reference_id,
        provider=x.provider,
        idempotency_key=storage_key,
        request_fingerprint=fingerprint,
        amount_minor=x.amount_minor,
        currency=x.currency,
        status=CREATED,
        correlation_id=x_correlation_id,
        created_by=ctx["sub"],
    )
    event = PaymentAttemptEvent(
        id=str(uuid.uuid4()),
        payment_attempt_id=attempt.id,
        tenant_id=ctx["tenant"],
        from_status=None,
        to_status=CREATED,
        event_type="payment_attempt.created",
        source="api",
        provider=attempt.provider,
        provider_account_reference=attempt.provider_account_reference,
        idempotency_key=storage_key,
        payload_digest=fingerprint,
        correlation_id=x_correlation_id,
        created_by=ctx["sub"],
    )
    try:
        s.add_all([attempt, event])
        audit(s, ctx, "billing.payment_attempt.created")
        s.commit()
    except IntegrityError:
        s.rollback()
        replay = s.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.tenant_id == ctx["tenant"], PaymentAttempt.idempotency_key == storage_key
            )
        )
        if replay and replay.request_fingerprint == fingerprint:
            IDEMPOTENT_REPLAY_TOTAL.labels(x.provider).inc()
            return _serialize(replay)
        CONFLICT_TOTAL.labels(x.provider).inc()
        raise HTTPException(409, "idempotency_key_payload_mismatch") from None
    CREATED_TOTAL.labels(x.provider).inc()
    return _serialize(attempt)


@router.get("/billing/payment-attempts")
def list_payment_attempts(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx=Depends(auth),
    s: Session = Depends(db),
):
    rows = s.scalars(
        select(PaymentAttempt)
        .where(PaymentAttempt.tenant_id == ctx["tenant"])
        .order_by(PaymentAttempt.created_at.desc(), PaymentAttempt.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_serialize(row) for row in rows]


def _tenant_attempt(s: Session, payment_attempt_id: str, tenant_id: str) -> PaymentAttempt:
    attempt = s.scalar(
        select(PaymentAttempt).where(PaymentAttempt.id == payment_attempt_id, PaymentAttempt.tenant_id == tenant_id)
    )
    if not attempt:
        raise HTTPException(404, "payment_attempt_not_found")
    return attempt


@router.get("/billing/payment-attempts/{payment_attempt_id}")
def get_payment_attempt(payment_attempt_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    return _serialize(_tenant_attempt(s, payment_attempt_id, ctx["tenant"]))


@router.get("/billing/payment-attempts/{payment_attempt_id}/events")
def get_payment_attempt_events(payment_attempt_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    _tenant_attempt(s, payment_attempt_id, ctx["tenant"])
    rows = s.scalars(
        select(PaymentAttemptEvent)
        .where(PaymentAttemptEvent.payment_attempt_id == payment_attempt_id, PaymentAttemptEvent.tenant_id == ctx["tenant"])
        .order_by(PaymentAttemptEvent.created_at.asc())
    ).all()
    return [_serialize_event(row) for row in rows]


def _apply_transition(
    s: Session,
    attempt: PaymentAttempt,
    target: str,
    *,
    event_type: str,
    source: str,
    ctx: dict,
    provider_event_reference: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    correlation_id: Optional[str] = None,
    failure_code: Optional[str] = None,
    failure_message: Optional[str] = None,
    next_action_type: Optional[str] = None,
    next_action_reference: Optional[str] = None,
) -> PaymentAttempt:
    """Centralized, transactional transition authority. Callers must already
    hold a row lock (``with_for_update``) on ``attempt``."""

    payload_digest = semantic_request_hash(
        action=event_type,
        resource="payment_attempt_events",
        payload={
            "to_status": target,
            "failure_code": failure_code,
            "failure_message": failure_message,
            "next_action_type": next_action_type,
            "next_action_reference": next_action_reference,
        },
    )
    if provider_event_reference:
        duplicate = s.scalar(
            select(PaymentAttemptEvent).where(
                PaymentAttemptEvent.provider == attempt.provider,
                PaymentAttemptEvent.provider_account_reference == attempt.provider_account_reference,
                PaymentAttemptEvent.provider_event_reference == provider_event_reference,
            )
        )
        if duplicate:
            if duplicate.payment_attempt_id != attempt.id or duplicate.payload_digest != payload_digest:
                CONFLICT_TOTAL.labels(attempt.provider).inc()
                raise PaymentAttemptConflict("provider_event_payload_mismatch")
            IDEMPOTENT_REPLAY_TOTAL.labels(attempt.provider).inc()
            return attempt

    if attempt.status == target:
        # Identical replay of an already-applied transition: no-op, no new event.
        IDEMPOTENT_REPLAY_TOTAL.labels(attempt.provider).inc()
        return attempt

    if attempt.status in TERMINAL_STATES or target not in TRANSITIONS.get(attempt.status, frozenset()):
        CONFLICT_TOTAL.labels(attempt.provider).inc()
        raise PaymentAttemptConflict("invalid_payment_attempt_transition")

    from_status = attempt.status
    attempt.status = target
    attempt.version += 1
    attempt.updated_at = now()
    timestamp_field = _TIMESTAMP_FIELD.get(target)
    if timestamp_field:
        setattr(attempt, timestamp_field, now())
    if failure_code is not None:
        attempt.failure_code = failure_code
    if failure_message is not None:
        attempt.failure_message = failure_message
    if next_action_type is not None:
        attempt.next_action_type = next_action_type
    if next_action_reference is not None:
        attempt.next_action_reference = next_action_reference

    event = PaymentAttemptEvent(
        id=str(uuid.uuid4()),
        payment_attempt_id=attempt.id,
        tenant_id=attempt.tenant_id,
        from_status=from_status,
        to_status=target,
        event_type=event_type,
        source=source,
        provider=attempt.provider,
        provider_account_reference=attempt.provider_account_reference,
        provider_event_reference=provider_event_reference,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        correlation_id=correlation_id or attempt.correlation_id,
        created_by=ctx["sub"],
    )
    s.add(event)
    TRANSITION_TOTAL.labels(f"{from_status}->{target}").inc()
    if target == FAILED:
        FAILED_TOTAL.labels(failure_code or "unknown").inc()
    return attempt


@router.post("/billing/payment-attempts/{payment_attempt_id}/cancel")
def cancel_payment_attempt(payment_attempt_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    attempt = s.scalar(
        select(PaymentAttempt)
        .where(PaymentAttempt.id == payment_attempt_id, PaymentAttempt.tenant_id == ctx["tenant"])
        .with_for_update()
    )
    if not attempt:
        raise HTTPException(404, "payment_attempt_not_found")
    _apply_transition(s, attempt, CANCELLED, event_type="payment_attempt.cancelled", source="api", ctx=ctx)
    audit(s, ctx, "billing.payment_attempt.cancelled")
    s.commit()
    return _serialize(attempt)


@router.post("/internal/billing/payment-attempts/{payment_attempt_id}/transition")
def transition_payment_attempt(
    payment_attempt_id: str,
    x: PaymentAttemptTransitionIn,
    ctx=Depends(require("platform_admin")),
    s: Session = Depends(db),
):
    attempt = s.scalar(
        select(PaymentAttempt).where(PaymentAttempt.id == payment_attempt_id).with_for_update()
    )
    if not attempt:
        raise HTTPException(404, "payment_attempt_not_found")
    if attempt.status != x.target_status and (
        attempt.status in TERMINAL_STATES
        or x.target_status not in TRANSITIONS.get(attempt.status, frozenset())
    ):
        raise PaymentAttemptConflict("invalid_payment_attempt_transition")
    if x.target_status in {AUTHORIZED, CAPTURED}:
        enforce_billing_capability(attempt.provider, require_live_charging=True)
    if x.target_status == CAPTURED and attempt.status != CAPTURED:
        # Re-validate under an invoice row lock so concurrent captures of
        # sibling attempts on the same invoice cannot collectively overpay it.
        invoice = _tenant_invoice(s, attempt.invoice_id, attempt.tenant_id, for_update=True)
        if invoice.status in {"PAID", "VOID", "CREDITED"}:
            raise HTTPException(409, "invoice_not_payable")
        remaining = _remaining_eligible_minor(s, invoice)
        if attempt.amount_minor > remaining:
            raise HTTPException(409, "amount_exceeds_remaining_balance")
    _apply_transition(
        s,
        attempt,
        x.target_status,
        event_type="payment_attempt.transition",
        source="internal",
        ctx=ctx,
        provider_event_reference=x.provider_event_reference,
        failure_code=x.failure_code,
        failure_message=x.failure_message,
        next_action_type=x.next_action_type,
        next_action_reference=x.next_action_reference,
    )
    audit(s, ctx, "billing.payment_attempt.transitioned")
    s.commit()
    return _serialize(attempt)
