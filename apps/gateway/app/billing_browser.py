"""Authenticated, read-only browser billing BFF for the customer portal."""
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .billing import (
    BillingPlan,
    BillingPrice,
    BillingSubscription,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentMethodReference,
    Refund,
    UsageEvent,
    Wallet,
    WalletTransaction,
)
from .main import db
from .tenancy import ROLE_PERMISSIONS

router = APIRouter(prefix="/app/api/billing", tags=["Browser billing"])


def browser_context_dependency(request: Request, s: Session = Depends(db)) -> dict[str, Any]:
    from .auth_bff import browser_context
    return browser_context(request=request, s=s)


def billing_context(ctx: dict[str, Any] = Depends(browser_context_dependency)) -> dict[str, Any]:
    role = str(ctx.get("role", "")).upper()
    permissions = ROLE_PERMISSIONS.get(role, set())
    if "*" not in permissions and "billing.read" not in permissions:
        raise HTTPException(403, "billing_read_required")
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
        "usage": [{"label": row.unit, "used": row.quantity, "unit": row.unit} for row in usage_rows[:20]],
    }


def invoice_summary(item: Invoice, paid: Decimal = Decimal("0"), refunded: Decimal = Decimal("0")) -> dict[str, Any]:
    return {
        "id": item.id,
        "reference": item.number,
        "status": item.status,
        "issued_at": iso(item.created_at),
        "due_at": iso(item.due_at),
        "total": decimal(item.total),
        "currency": item.currency,
        "amount_due": decimal(Decimal(item.total) - Decimal(paid) - Decimal(item.credits)),
        "amount_paid": decimal(paid),
        "amount_refunded": decimal(refunded),
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


@router.get("/overview")
def overview(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    tenant = ctx["tenant"]
    subscription = s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == tenant))
    invoices = s.scalars(select(Invoice).where(Invoice.tenant_id == tenant).order_by(Invoice.created_at.desc())).all()
    payments = s.scalars(select(Payment).where(Payment.tenant_id == tenant).order_by(Payment.created_at.desc()).limit(5)).all()
    wallet = s.get(Wallet, tenant)
    paid = s.scalar(select(func.sum(Payment.amount)).where(Payment.tenant_id == tenant, Payment.status == "CONFIRMED")) or 0
    refunded = s.scalar(select(func.sum(Refund.amount)).where(Refund.tenant_id == tenant, Refund.status == "CONFIRMED")) or 0
    outstanding = sum((Decimal(item.total) - Decimal(item.credits) for item in invoices if item.status not in {"PAID", "VOID", "CREDITED"}), Decimal("0")) - Decimal(paid) + Decimal(refunded)
    invoice_refs = {item.id: item.number for item in invoices}
    return {
        "subscription": subscription_payload(s, subscription),
        "outstanding_balance": decimal(max(outstanding, Decimal("0"))),
        "currency": invoices[0].currency if invoices else (wallet.currency if wallet else "USD"),
        "wallet_balance": decimal(wallet.balance if wallet else 0),
        "most_recent_invoice": invoice_summary(invoices[0]) if invoices else None,
        "recent_payments": [payment_summary(item, invoice_refs.get(item.invoice_id)) for item in payments],
        "capabilities": [{"key": "historical_billing", "available": True, "reason": "available"}],
    }


@router.get("/subscription")
def subscription(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    item = s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == ctx["tenant"]))
    if not item:
        raise HTTPException(404, "subscription_not_found")
    return subscription_payload(s, item) or {}


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
    return {"items": [invoice_summary(item) for item in rows[:limit]], "limit": limit, "offset": offset, "has_more": more}


@router.get("/invoices/{invoice_id}")
def invoice_detail(invoice_id: str, ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    item = s.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == ctx["tenant"]))
    if not item:
        raise HTTPException(404, "invoice_not_found")
    paid = s.scalar(select(func.sum(Payment.amount)).where(Payment.invoice_id == item.id, Payment.status == "CONFIRMED")) or 0
    refunded = s.scalar(select(func.sum(Refund.amount)).where(Refund.payment_id.in_(select(Payment.id).where(Payment.invoice_id == item.id)), Refund.status == "CONFIRMED")) or 0
    result = invoice_summary(item, paid, refunded)
    result["line_items"] = [{"description": line.description, "quantity": line.quantity, "unit_amount": decimal(line.unit_amount), "amount": decimal(line.amount), "currency": item.currency} for line in s.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == item.id)).all()]
    result["billing_identity"] = None
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


@router.get("/wallet")
def wallet(ctx: dict[str, Any] = Depends(billing_context), s: Session = Depends(db)) -> dict[str, Any]:
    item = s.get(Wallet, ctx["tenant"])
    transactions = s.scalars(select(WalletTransaction).where(WalletTransaction.tenant_id == ctx["tenant"]).order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc()).limit(100)).all()
    currency = item.currency if item else (transactions[0].currency if transactions else "USD")
    return {"balance": decimal(item.balance if item else 0), "currency": currency, "transactions": [{"id": row.id, "type": row.kind, "status": "posted", "created_at": iso(row.created_at), "amount": decimal(row.amount), "currency": row.currency, "description": row.reference} for row in transactions]}
