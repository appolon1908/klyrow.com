"""Authenticated browser billing BFF for the customer portal."""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .billing import (
    BillingPlan,
    BillingPrice,
    BillingSubscription,
    BillingEvent,
    now,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentMethodReference,
    Refund,
    UsageEvent,
    Wallet,
    WalletTransaction,
)
from .billing_ledger import invoice_balance
from .billing_entitlements import SubscriptionState, calculate_entitlements, require_version
from .billing_entitlements import SubscriptionSnapshot, transition as apply_subscription_transition
from .billing_proration import quote_plan_change
from .billing import enqueue_subscription_changed
from .auth_bff import csrf_guard
from .billing_checkout import create_or_resume_stripe_checkout
from .billing_config import BillingConfigError, load_billing_settings
from .main import db
from .tenancy import ROLE_PERMISSIONS

router = APIRouter(prefix="/app/api/billing", tags=["Browser billing"])


class SubscriptionQuoteIn(BaseModel):
    plan_code: str = Field(min_length=2, max_length=40)


class SubscriptionLifecycleIn(BaseModel):
    expected_version: int = Field(ge=1)


class SubscriptionChangeIn(SubscriptionLifecycleIn):
    plan_code: str = Field(min_length=2, max_length=40)


def _tenant_subscription(s: Session, tenant_id: str) -> BillingSubscription:
    item = s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == tenant_id).with_for_update())
    if item is None:
        raise HTTPException(404, "subscription_not_found")
    return item


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _subscription_entitlements(s: Session, item: BillingSubscription) -> dict[str, Any]:
    plan = s.get(BillingPlan, item.plan_id)
    usage_rows = s.scalars(select(UsageEvent).where(UsageEvent.tenant_id == item.tenant_id)).all()
    usage: dict[str, int] = {}
    for row in usage_rows:
        usage[row.unit] = usage.get(row.unit, 0) + row.quantity
    return calculate_entitlements(
        state=SubscriptionState(item.status),
        features=json.loads(plan.features_json or "{}") if plan else {},
        usage=usage,
    )


def browser_context_dependency(request: Request, s: Session = Depends(db)) -> dict[str, Any]:
    from .auth_bff import browser_context
    return browser_context(request=request, s=s)


def billing_context(ctx: dict[str, Any] = Depends(browser_context_dependency)) -> dict[str, Any]:
    role = str(ctx.get("role", "")).upper()
    permissions = ROLE_PERMISSIONS.get(role, set())
    if "*" not in permissions and "billing.read" not in permissions:
        raise HTTPException(403, "billing_read_required")
    return ctx


def _has_permission(ctx: dict[str, Any], permission: str) -> bool:
    role = str(ctx.get("role", "")).upper()
    permissions = ROLE_PERMISSIONS.get(role, set())
    return "*" in permissions or permission in permissions


def billing_manage_context(
    request: Request,
    current=Depends(csrf_guard),
    s: Session = Depends(db),
) -> dict[str, Any]:
    ctx = browser_context_dependency(request=request, s=s)
    if not _has_permission(ctx, "billing.manage"):
        raise HTTPException(403, "billing_manage_required")
    return ctx


def decimal(value: Any) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def subscription_payload(s: Session, item: Optional[BillingSubscription]) -> Optional[dict[str, Any]]:
    if not item:
        return None
    plan = s.get(BillingPlan, item.plan_id)
    price = s.get(BillingPrice, item.price_id)
    usage_rows = s.scalars(
        select(UsageEvent).where(UsageEvent.tenant_id == item.tenant_id).order_by(UsageEvent.occurred_at.desc()).limit(500)
    ).all()
    usage = {}
    for row in usage_rows:
        usage[row.unit] = usage.get(row.unit, 0) + row.quantity
    features = json.loads(plan.features_json or "{}") if plan else {}
    return {
        "product": "Klyrow Email",
        "plan": plan.name if plan else item.plan_id,
        "status": item.status,
        "interval": price.billing_cycle if price else "UNKNOWN",
        "trial_end": iso(item.trial_end),
        "price": decimal(price.base_amount) if price else None,
        "currency": price.currency if price else None,
        "renews_at": iso(item.period_end),
        "cancels_at": iso(item.period_end) if item.cancel_at_period_end else None,
        "entitlements": calculate_entitlements(
            state=SubscriptionState(item.status),
            features=features,
            usage=usage,
        ),
        "usage": [{"label": row.unit, "used": row.quantity, "unit": row.unit} for row in usage_rows[:20]],
    }


def financial_invoice_summary(s: Session, item: Invoice) -> dict[str, Any]:
    balance = invoice_balance(s, item)
    return {
        "id": item.id,
        "reference": item.number,
        "status": balance.financial_status,
        "issued_at": iso(item.created_at),
        "due_at": iso(item.due_at),
        "total": decimal(balance.total),
        "currency": item.currency,
        "amount_due": decimal(balance.remaining_due),
        "amount_paid": decimal(balance.settled),
        "amount_refunded": decimal(balance.refunded),
    }


def payment_summary(item: Payment, invoice_reference: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": item.id,
        "reference": item.provider_reference,
        "status": item.status,
        "created_at": iso(item.created_at),
        "amount": decimal(item.amount),
        "currency": item.currency,
        "invoice_reference": invoice_reference,
    }


@router.get("/overview", operation_id="billing_browser_overview_get")
def overview(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    tenant = ctx["tenant"]
    subscription = s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == tenant))
    invoices = s.scalars(select(Invoice).where(Invoice.tenant_id == tenant).order_by(Invoice.created_at.desc())).all()
    payments = s.scalars(select(Payment).where(Payment.tenant_id == tenant).order_by(Payment.created_at.desc()).limit(5)).all()
    wallet = s.get(Wallet, tenant)
    outstanding_by_currency: dict[str, Decimal] = {}
    for item in invoices:
        balance = invoice_balance(s, item)
        outstanding_by_currency[item.currency] = outstanding_by_currency.get(item.currency, Decimal("0")) + balance.remaining_due
    display_currency = invoices[0].currency if invoices else (wallet.currency if wallet else "USD")
    invoice_refs = {item.id: item.number for item in invoices}
    return {
        "subscription": subscription_payload(s, subscription),
        "outstanding_balance": decimal(outstanding_by_currency.get(display_currency, Decimal("0"))),
        "currency": display_currency,
        "wallet_balance": decimal(wallet.balance if wallet else 0),
        "most_recent_invoice": financial_invoice_summary(s, invoices[0]) if invoices else None,
        "recent_payments": [payment_summary(item, invoice_refs.get(item.invoice_id)) for item in payments],
        "capabilities": [{"key": "historical_billing", "available": True, "reason": "available"}],
        "outstanding_by_currency": [
            {"currency": currency, "amount_due": decimal(amount)}
            for currency, amount in sorted(outstanding_by_currency.items())
        ],
    }


@router.get("/subscription")
def subscription(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    item = s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == ctx["tenant"]))
    if not item:
        raise HTTPException(404, "subscription_not_found")
    return subscription_payload(s, item) or {}


@router.get("/catalog")
def catalog(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    plans = s.scalars(select(BillingPlan).where(BillingPlan.active.is_(True)).order_by(BillingPlan.code)).all()
    items = []
    for plan in plans:
        price = s.scalar(select(BillingPrice).where(BillingPrice.plan_id == plan.id, BillingPrice.retired_at.is_(None)).order_by(BillingPrice.version.desc()))
        if price is None:
            continue
        items.append({
            "code": plan.code,
            "name": plan.name,
            "features": json.loads(plan.features_json or "{}"),
            "price_version": price.version,
            "currency": price.currency,
            "billing_cycle": price.billing_cycle,
            "base_amount": decimal(price.base_amount),
            "included_units": price.included_units,
            "overage_amount": decimal(price.overage_amount),
        })
    return {"items": items}


@router.get("/entitlements")
def entitlements(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    item = s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == ctx["tenant"]))
    if item is None:
        raise HTTPException(404, "subscription_not_found")
    return {"status": item.status, "version": item.version, "entitlements": _subscription_entitlements(s, item)}


@router.post("/subscription/quote")
def subscription_quote(payload: SubscriptionQuoteIn, ctx: dict[str, Any] = Depends(billing_manage_context), s: Session = Depends(db)) -> dict[str, Any]:
    item = s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == ctx["tenant"]))
    if item is None:
        raise HTTPException(404, "subscription_not_found")
    plan = s.scalar(select(BillingPlan).where(BillingPlan.code == payload.plan_code, BillingPlan.active.is_(True)))
    price = s.scalar(select(BillingPrice).where(BillingPrice.plan_id == plan.id, BillingPrice.retired_at.is_(None)).order_by(BillingPrice.version.desc())) if plan else None
    old = s.get(BillingPrice, item.price_id)
    if plan is None or price is None or old is None:
        raise HTTPException(404, "active_plan_price_not_found")
    quote = quote_plan_change(old_price=old.base_amount, new_price=price.base_amount, period_start=_utc(item.period_start), period_end=_utc(item.period_end), at=now())
    return {"current_plan": item.plan_id, "target_plan": plan.code, "target_price_version": price.version, "effective": quote.effective, "charge": str(quote.charge), "credit": str(quote.credit), "fraction_remaining": str(quote.fraction_remaining)}


@router.post("/subscription/change")
def change_subscription(payload: SubscriptionChangeIn, ctx: dict[str, Any] = Depends(billing_manage_context), s: Session = Depends(db)) -> dict[str, Any]:
    item = _tenant_subscription(s, ctx["tenant"])
    try:
        require_version(SubscriptionSnapshot(state=SubscriptionState(item.status), version=item.version, period_end=item.period_end), payload.expected_version)
    except ValueError:
        raise HTTPException(409, "subscription_version_conflict") from None
    plan = s.scalar(select(BillingPlan).where(BillingPlan.code == payload.plan_code, BillingPlan.active.is_(True)))
    price = s.scalar(select(BillingPrice).where(BillingPrice.plan_id == plan.id, BillingPrice.retired_at.is_(None)).order_by(BillingPrice.version.desc())) if plan else None
    old = s.get(BillingPrice, item.price_id)
    if plan is None or price is None or old is None:
        raise HTTPException(404, "active_plan_price_not_found")
    quote = quote_plan_change(old_price=old.base_amount, new_price=price.base_amount, period_start=_utc(item.period_start), period_end=_utc(item.period_end), at=now())
    if quote.effective == "NEXT_PERIOD":
        raise HTTPException(409, "downgrade_requires_period_end")
    item.plan_id = plan.id
    item.price_id = price.id
    item.version += 1
    event = BillingEvent(id=str(uuid.uuid4()), tenant_id=item.tenant_id, kind="subscription.plan_changed", reference=item.id, payload_json=json.dumps({"old_price_id": old.id, "new_price_id": price.id, "proration_charge": str(quote.charge)}, sort_keys=True))
    s.add(event)
    enqueue_subscription_changed(s, item, causation_id=event.id)
    s.commit()
    return {"id": item.id, "status": item.status, "version": item.version, "effective": quote.effective, "charge": str(quote.charge), "credit": str(quote.credit)}


def _change_browser_subscription(target: SubscriptionState, expected_version: int, ctx: dict[str, Any], s: Session) -> dict[str, Any]:
    item = _tenant_subscription(s, ctx["tenant"])
    try:
        require_version(SubscriptionSnapshot(state=SubscriptionState(item.status), version=item.version, period_end=item.period_end), expected_version)
    except ValueError:
        raise HTTPException(409, "subscription_version_conflict") from None
    try:
        updated = apply_subscription_transition(SubscriptionSnapshot(state=SubscriptionState(item.status), version=item.version, period_end=item.period_end, trial_end=item.trial_end, cancel_at_period_end=item.cancel_at_period_end), target)
    except (ValueError, KeyError):
        raise HTTPException(409, "invalid_subscription_transition") from None
    item.status = updated.state.value
    item.version = updated.version
    item.cancel_at_period_end = updated.cancel_at_period_end
    event = BillingEvent(id=str(uuid.uuid4()), tenant_id=item.tenant_id, kind=f"subscription.{target.value.lower()}", reference=item.id)
    s.add(event)
    enqueue_subscription_changed(s, item, causation_id=event.id)
    s.commit()
    return {"id": item.id, "status": item.status, "version": item.version}


@router.post("/subscription/cancel")
def cancel_subscription(payload: SubscriptionLifecycleIn, ctx: dict[str, Any] = Depends(billing_manage_context), s: Session = Depends(db)) -> dict[str, Any]:
    return _change_browser_subscription(SubscriptionState.CANCEL_AT_PERIOD_END, payload.expected_version, ctx, s)


@router.post("/subscription/reactivate")
def reactivate_subscription(payload: SubscriptionLifecycleIn, ctx: dict[str, Any] = Depends(billing_manage_context), s: Session = Depends(db)) -> dict[str, Any]:
    return _change_browser_subscription(SubscriptionState.ACTIVE, payload.expected_version, ctx, s)


@router.get("/invoices")
def invoices(
    ctx: dict[str, Any] = Depends(billing_context),
    s: Session = Depends(db),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    query = select(Invoice).where(Invoice.tenant_id == ctx["tenant"])
    if status:
        query = query.where(Invoice.status == status.upper())
    rows = s.scalars(query.order_by(Invoice.created_at.desc(), Invoice.id.desc()).offset(offset).limit(limit + 1)).all()
    more = len(rows) > limit
    return {"items": [financial_invoice_summary(s, item) for item in rows[:limit]], "limit": limit, "offset": offset, "has_more": more}


@router.get("/invoices/{invoice_id}")
def invoice_detail(invoice_id: str, ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    from .payment_attempts import ACTIVE_CHECKOUT_STATES, PaymentAttempt

    item = s.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == ctx["tenant"]))
    if not item:
        raise HTTPException(404, "invoice_not_found")
    result = financial_invoice_summary(s, item)
    result["line_items"] = [{"description": line.description, "quantity": line.quantity, "unit_amount": decimal(line.unit_amount), "amount": decimal(line.amount), "currency": item.currency} for line in s.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == item.id)).all()]
    result["billing_identity"] = None
    result["active_checkout"] = s.scalar(select(PaymentAttempt.id).where(
        PaymentAttempt.tenant_id == ctx["tenant"],
        PaymentAttempt.invoice_id == item.id,
        PaymentAttempt.provider == "stripe",
        PaymentAttempt.status.in_(ACTIVE_CHECKOUT_STATES),
    )) is not None
    return result


@router.get("/payments")
def payments(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    rows = s.scalars(select(Payment).where(Payment.tenant_id == ctx["tenant"]).order_by(Payment.created_at.desc(), Payment.id.desc()).offset(offset).limit(limit + 1)).all()
    more = len(rows) > limit
    refs = {item.id: item.number for item in s.scalars(select(Invoice).where(Invoice.tenant_id == ctx["tenant"])).all()}
    return {"items": [payment_summary(item, refs.get(item.invoice_id)) for item in rows[:limit]], "limit": limit, "offset": offset, "has_more": more}


@router.get("/refunds")
def refunds(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    rows = s.scalars(select(Refund).where(Refund.tenant_id == ctx["tenant"]).order_by(Refund.created_at.desc(), Refund.id.desc()).offset(offset).limit(limit + 1)).all()
    more = len(rows) > limit
    return {"items": [{"id": item.id, "reference": item.provider_reference, "status": item.status, "created_at": iso(item.created_at), "amount": decimal(item.amount), "currency": payment.currency if (payment := s.get(Payment, item.payment_id)) else "USD", "payment_reference": payment.provider_reference if payment else None} for item in rows[:limit]], "limit": limit, "offset": offset, "has_more": more}


@router.get("/payment-methods")
def payment_methods(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> list[dict[str, Any]]:
    rows = s.scalars(select(PaymentMethodReference).where(PaymentMethodReference.tenant_id == ctx["tenant"], PaymentMethodReference.revoked_at.is_(None))).all()
    return [{"id": item.id, "type": item.provider, "display": item.label, "brand": None, "last4": None, "expires_at": None, "status": "default" if item.is_default else "active"} for item in rows]


@router.get("/capabilities")
def capabilities(ctx: dict[str, Any] = Depends(billing_context)) -> dict[str, Any]:
    try:
        settings = load_billing_settings()
    except BillingConfigError:
        return {
            "billing_enabled": False,
            "checkout_enabled": False,
            "stripe": {"available": False, "environment": "sandbox"},
            "live_charging": False,
        }
    stripe_available = (
        settings.enabled
        and settings.webhook_processing_enabled
        and settings.stripe.enabled
        and settings.stripe.environment == "sandbox"
    )
    return {
        "billing_enabled": settings.enabled,
        "checkout_enabled": stripe_available and _has_permission(ctx, "billing.manage"),
        "stripe": {"available": stripe_available, "environment": settings.stripe.environment},
        "live_charging": settings.live_charging_enabled,
    }


@router.post("/invoices/{invoice_id}/checkout", status_code=201)
def checkout_invoice(
    invoice_id: str,
    ctx: dict[str, Any] = Depends(billing_manage_context),
    s: Session = Depends(db),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
) -> dict[str, Any]:
    result = create_or_resume_stripe_checkout(
        s,
        tenant_id=ctx["tenant"],
        actor_id=ctx["sub"],
        invoice_id=invoice_id,
        idempotency_key=idempotency_key,
    )
    return {
        "payment_attempt_id": result.attempt_id,
        "invoice_id": result.invoice_id,
        "status": result.status,
        "hosted_checkout_url": result.checkout_url,
        "expires_at": iso(result.expires_at),
    }


@router.get("/payment-attempts/{payment_attempt_id}")
def browser_payment_attempt(
    payment_attempt_id: str,
    ctx: dict[str, Any] = Depends(billing_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    from .payment_attempts import PaymentAttempt

    attempt = s.scalar(select(PaymentAttempt).where(
        PaymentAttempt.id == payment_attempt_id,
        PaymentAttempt.tenant_id == ctx["tenant"],
    ))
    if attempt is None:
        raise HTTPException(404, "payment_attempt_not_found")
    return {
        "id": attempt.id,
        "invoice_id": attempt.invoice_id,
        "provider": attempt.provider,
        "status": attempt.status,
        "next_action_type": attempt.next_action_type,
        "expires_at": iso(attempt.expires_at),
        "failure_code": attempt.failure_code,
        "created_at": iso(attempt.created_at),
        "updated_at": iso(attempt.updated_at),
        "next_action_reference": attempt.next_action_reference if attempt.next_action_type == "hosted_checkout" else None,
    }


@router.get("/wallet")
def wallet(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    item = s.get(Wallet, ctx["tenant"])
    transactions = s.scalars(select(WalletTransaction).where(WalletTransaction.tenant_id == ctx["tenant"]).order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc()).limit(100)).all()
    currency = item.currency if item else (transactions[0].currency if transactions else "USD")
    return {"balance": decimal(item.balance if item else 0), "currency": currency, "transactions": [{"id": row.id, "type": row.kind, "status": "posted", "created_at": iso(row.created_at), "amount": decimal(row.amount), "currency": row.currency, "description": row.reference} for row in transactions]}
