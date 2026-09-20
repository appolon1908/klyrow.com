"""Read-only browser billing BFF backed by the canonical ledger."""
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .billing import BillingSubscription, Invoice, InvoiceLine, Payment, PaymentMethodReference, Refund, Wallet
from .billing_ledger import invoice_balance
from .main import db
from .tenancy import ROLE_PERMISSIONS

router = APIRouter(prefix="/app/api/billing", tags=["Browser billing"])


def browser_billing_context(request: Request, session: Session = Depends(db)) -> dict[str, Any]:
    from .auth_bff import browser_context
    ctx = browser_context(request=request, s=session)
    permissions = ROLE_PERMISSIONS.get(str(ctx.get("role") or "").upper(), set())
    if "*" not in permissions and "billing.read" not in permissions:
        raise HTTPException(403, "billing_read_required")
    return ctx


def _decimal(value: Decimal) -> str: return str(Decimal(value).quantize(Decimal("0.01")))
def _iso(value): return value.isoformat() if value else None


def invoice_payload(session: Session, invoice: Invoice) -> dict[str, Any]:
    balance = invoice_balance(session, invoice)
    return {"id": invoice.id, "reference": invoice.number, "status": balance.financial_status,
            "issued_at": _iso(invoice.created_at), "due_at": _iso(invoice.due_at),
            "total": _decimal(balance.total), "credits": _decimal(balance.credits),
            "amount_paid": _decimal(balance.settled), "amount_refunded": _decimal(balance.refunded),
            "amount_due": _decimal(balance.remaining_due), "currency": invoice.currency}


@router.get("/overview")
def overview(ctx: dict = Depends(browser_billing_context), session: Session = Depends(db)):
    invoices = session.scalars(select(Invoice).where(Invoice.tenant_id == ctx["tenant"]).order_by(Invoice.created_at.desc())).all()
    balances: dict[str, Decimal] = {}
    for invoice in invoices:
        balances[invoice.currency] = balances.get(invoice.currency, Decimal("0")) + invoice_balance(session, invoice).remaining_due
    subscription = session.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == ctx["tenant"]))
    return {"subscription": {"status": subscription.status, "renews_at": _iso(subscription.period_end)} if subscription else None,
            "outstanding_by_currency": [{"currency": currency, "amount_due": _decimal(amount)} for currency, amount in sorted(balances.items())],
            "most_recent_invoice": invoice_payload(session, invoices[0]) if invoices else None}


@router.get("/invoices")
def invoices(ctx: dict = Depends(browser_billing_context), session: Session = Depends(db), status: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    query = select(Invoice).where(Invoice.tenant_id == ctx["tenant"])
    if status: query = query.where(Invoice.status == status.upper())
    rows = session.scalars(query.order_by(Invoice.created_at.desc(), Invoice.id.desc()).offset(offset).limit(limit + 1)).all()
    return {"items": [invoice_payload(session, item) for item in rows[:limit]], "limit": limit, "offset": offset, "has_more": len(rows) > limit}


@router.get("/invoices/{invoice_id}")
def invoice_detail(invoice_id: str, ctx: dict = Depends(browser_billing_context), session: Session = Depends(db)):
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == ctx["tenant"]))
    if not invoice: raise HTTPException(404, "invoice_not_found")
    result = invoice_payload(session, invoice)
    result["line_items"] = [{"description": line.description, "quantity": line.quantity, "amount": _decimal(line.amount), "currency": invoice.currency} for line in session.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))]
    return result


@router.get("/payments")
def payments(ctx: dict = Depends(browser_billing_context), session: Session = Depends(db)):
    return {"items": [{"id": row.id, "reference": row.provider_reference, "status": row.status, "amount": _decimal(row.amount), "currency": row.currency, "created_at": _iso(row.created_at)} for row in session.scalars(select(Payment).where(Payment.tenant_id == ctx["tenant"]).order_by(Payment.created_at.desc()))]}


@router.get("/refunds")
def refunds(ctx: dict = Depends(browser_billing_context), session: Session = Depends(db)):
    return {"items": [{"id": row.id, "reference": row.provider_reference, "status": row.status, "amount": _decimal(row.amount), "created_at": _iso(row.created_at)} for row in session.scalars(select(Refund).where(Refund.tenant_id == ctx["tenant"]).order_by(Refund.created_at.desc()))]}


@router.get("/payment-methods")
def payment_methods(ctx: dict = Depends(browser_billing_context), session: Session = Depends(db)):
    return {"items": [{"id": row.id, "type": row.provider, "display": row.label, "status": "default" if row.is_default else "active"} for row in session.scalars(select(PaymentMethodReference).where(PaymentMethodReference.tenant_id == ctx["tenant"], PaymentMethodReference.revoked_at.is_(None)))]}


@router.get("/wallet")
def wallet(ctx: dict = Depends(browser_billing_context), session: Session = Depends(db)):
    item = session.get(Wallet, ctx["tenant"])
    return {"balance": _decimal(item.balance) if item else "0.00", "currency": item.currency if item else "USD", "transactions": []}


@router.get("/subscription")
def subscription(ctx: dict = Depends(browser_billing_context), session: Session = Depends(db)):
    item = session.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id == ctx["tenant"]))
    if not item: raise HTTPException(404, "subscription_not_found")
    return {"status": item.status, "renews_at": _iso(item.period_end)}