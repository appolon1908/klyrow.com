"""Same-origin browser BFF for Klyrow platform administration.

This module exposes bounded, operator-safe browser contracts and reuses the
existing platform authorities directly in-process.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .billing import (
    BillingPlan,
    BillingPrice,
    BillingSubscription,
    BillingWorkItem,
    DunningIn,
    Invoice,
    Payment,
    Refund,
    dunning as run_dunning,
)
from .billing_config import BillingConfigError, load_billing_settings
from .billing_reconciliation import reconcile_billing
from .delivery_controls import (
    AbuseAlert,
    AbuseIn,
    ResourceSuspension,
    SuspendIn,
    evaluate_abuse,
    release_resource,
    suspend_resource,
)
from .main import Audit, Tenant, User, audit, db
from .operations import ReconciliationRun, reconcile as run_platform_reconciliation

router = APIRouter(tags=["Browser platform administration"])


def admin_browser_context(request: Request, session: Session = Depends(db)) -> dict:
    """Resolve the existing browser context lazily to avoid import-order coupling."""
    from .auth_bff import browser_context

    return browser_context(request=request, s=session)


def admin_csrf_guard(
    request: Request,
    x_klyrow_csrf: str = Header(default="", alias="X-Klyrow-CSRF"),
    session: Session = Depends(db),
):
    """Delegate CSRF enforcement to the canonical browser-session authority."""
    from .auth_bff import csrf_guard

    return csrf_guard(request=request, x_klyrow_csrf=x_klyrow_csrf, s=session)


class AbuseAlertStateIn(BaseModel):
    state: str = Field(pattern="^(ACKNOWLEDGED|RESOLVED)$")


class AdminDunningIn(BaseModel):
    grace_days: int = Field(default=7, ge=1, le=90)
    suspend_days: int = Field(default=21, ge=2, le=180)


class AdminAbuseSummaryOut(BaseModel):
    open_alerts: int
    critical_open: int
    active_suspensions: int


class AdminAbuseAlertOut(BaseModel):
    id: str
    tenant_id: str
    tenant_name: Optional[str] = None
    kind: str
    severity: str
    state: str
    metrics: dict[str, Any]
    created_at: datetime


class AdminSuspensionOut(BaseModel):
    id: str
    tenant_id: str
    tenant_name: Optional[str] = None
    resource_type: str
    resource_id: str
    reason: str
    created_by: str
    created_at: datetime
    active: bool


class AdminAbuseStateOut(BaseModel):
    summary: AdminAbuseSummaryOut
    alerts: list[AdminAbuseAlertOut]
    suspensions: list[AdminSuspensionOut]


class AdminAbuseEvaluationOut(BaseModel):
    id: Optional[str] = None
    state: str
    suspended: bool


class AdminSuspensionCreateOut(BaseModel):
    id: str
    effective_immediately: bool


class AdminSuspensionReleaseOut(BaseModel):
    id: str
    active: bool
    effective_immediately: bool


class AdminAlertStateOut(BaseModel):
    id: str
    state: str


class AdminReconciliationRunOut(BaseModel):
    id: str
    tenant_id: Optional[str] = None
    kind: str
    state: str
    drift_count: int
    detail_count: int
    started_at: datetime
    completed_at: Optional[datetime] = None


class AdminReconciliationListOut(BaseModel):
    runs: list[AdminReconciliationRunOut]


class AdminReconciliationDetailOut(AdminReconciliationRunOut):
    details: list[dict[str, Any]]


class AdminReconciliationExecutionOut(BaseModel):
    id: str
    state: str
    drift_count: int
    auto_corrected: bool


class AdminBillingIssueOut(BaseModel):
    code: str
    tenant_id: str
    resource_id: Optional[str] = None
    details: dict[str, Any]


class AdminBillingReconciliationOut(BaseModel):
    tenant_id: Optional[str] = None
    status: str
    issue_count: int
    issues: list[AdminBillingIssueOut]
    auto_corrected: bool


class AdminBillingConfigurationOut(BaseModel):
    valid: bool
    error: Optional[str] = None
    enabled: bool
    live_charging_enabled: bool
    dunning_enabled: bool
    refunds_enabled: bool
    reconciliation_enabled: bool
    providers: dict[str, str]


class AdminBillingDriftOut(BaseModel):
    status: str
    issue_count: int


class AdminActivePriceOut(BaseModel):
    price_id: str
    plan_id: str
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None
    version: int
    currency: str
    billing_cycle: str
    base_amount: str
    included_units: int
    overage_amount: str
    effective_at: datetime


class AdminBillingInvoiceOut(BaseModel):
    id: str
    number: str
    tenant_id: str
    tenant_name: Optional[str] = None
    status: str
    total: str
    currency: str
    due_at: datetime
    created_at: datetime


class AdminBillingOverviewOut(BaseModel):
    configuration: AdminBillingConfigurationOut
    counts: dict[str, dict[str, int]]
    billing_drift: AdminBillingDriftOut
    active_prices: list[AdminActivePriceOut]
    recent_invoices: list[AdminBillingInvoiceOut]


class AdminBillingSubscriptionOut(BaseModel):
    id: str
    tenant_id: str
    tenant_name: Optional[str] = None
    status: str
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None
    billing_cycle: Optional[str] = None
    currency: Optional[str] = None
    period_start: datetime
    period_end: datetime
    trial_end: Optional[datetime] = None
    cancel_at_period_end: bool
    version: int
    open_invoice_count: int


class AdminDunningItemOut(BaseModel):
    invoice_id: str
    subscription_status: str


class AdminDunningOut(BaseModel):
    processed: int
    items: list[AdminDunningItemOut]
    login_disabled: bool


class AdminAuditEntryOut(BaseModel):
    id: str
    tenant_id: str
    tenant_name: Optional[str] = None
    actor: str
    action: str
    created_at: datetime


class AdminAuditPageOut(BaseModel):
    items: list[AdminAuditEntryOut]
    next_cursor: Optional[str] = None


def _require_platform_admin(ctx: dict, session: Session) -> dict:
    user = session.get(User, ctx.get("sub"))
    if (
        user is None
        or not user.enabled
        or str(user.role or "").lower() != "platform_admin"
    ):
        raise HTTPException(403, "platform_admin_required")
    return ctx


def _tenant_name(session: Session, tenant_id: str | None) -> str | None:
    if not tenant_id:
        return None
    tenant = session.get(Tenant, tenant_id)
    return tenant.name if tenant else None


def _safe_json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_json_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _count_by(session: Session, model, field) -> dict[str, int]:
    rows = session.execute(select(field, func.count()).select_from(model).group_by(field)).all()
    return {str(key or "UNKNOWN"): int(count or 0) for key, count in rows}


def _decimal(value: Decimal | int | float | str | None) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _billing_configuration() -> dict[str, Any]:
    try:
        settings = load_billing_settings()
    except BillingConfigError as exc:
        return {
            "valid": False,
            "error": exc.code,
            "enabled": False,
            "live_charging_enabled": False,
            "dunning_enabled": False,
            "refunds_enabled": False,
            "reconciliation_enabled": False,
            "providers": {},
        }
    return {
        "valid": True,
        "error": None,
        "enabled": settings.enabled,
        "live_charging_enabled": settings.live_charging_enabled,
        "dunning_enabled": settings.dunning_enabled,
        "refunds_enabled": settings.refunds_enabled,
        "reconciliation_enabled": settings.reconciliation_enabled,
        "providers": settings.capability_status().providers,
    }


@router.get("/app/api/admin/abuse", response_model=AdminAbuseStateOut)
def browser_admin_abuse(
    state: Optional[str] = Query(default=None, min_length=1, max_length=40),
    tenant_id: Optional[str] = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: dict = Depends(admin_browser_context),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    alert_query = select(AbuseAlert)
    if state:
        alert_query = alert_query.where(AbuseAlert.state == state.upper())
    if tenant_id:
        alert_query = alert_query.where(AbuseAlert.tenant_id == tenant_id)
    alerts = session.scalars(
        alert_query.order_by(AbuseAlert.created_at.desc(), AbuseAlert.id.desc()).limit(limit)
    ).all()

    suspension_query = select(ResourceSuspension).where(ResourceSuspension.active == True)
    if tenant_id:
        suspension_query = suspension_query.where(ResourceSuspension.tenant_id == tenant_id)
    suspensions = session.scalars(
        suspension_query.order_by(
            ResourceSuspension.created_at.desc(), ResourceSuspension.id.desc()
        ).limit(limit)
    ).all()

    open_alerts = session.scalar(
        select(func.count()).select_from(AbuseAlert).where(AbuseAlert.state == "OPEN")
    ) or 0
    critical_open = session.scalar(
        select(func.count()).select_from(AbuseAlert).where(
            AbuseAlert.state == "OPEN", AbuseAlert.severity == "CRITICAL"
        )
    ) or 0
    active_suspensions = session.scalar(
        select(func.count()).select_from(ResourceSuspension).where(
            ResourceSuspension.active == True
        )
    ) or 0

    return {
        "summary": {
            "open_alerts": int(open_alerts),
            "critical_open": int(critical_open),
            "active_suspensions": int(active_suspensions),
        },
        "alerts": [
            {
                "id": item.id,
                "tenant_id": item.tenant_id,
                "tenant_name": _tenant_name(session, item.tenant_id),
                "kind": item.kind,
                "severity": item.severity,
                "state": item.state,
                "metrics": _safe_json_object(item.metrics_json),
                "created_at": item.created_at,
            }
            for item in alerts
        ],
        "suspensions": [
            {
                "id": item.id,
                "tenant_id": item.tenant_id,
                "tenant_name": _tenant_name(session, item.tenant_id),
                "resource_type": item.resource_type,
                "resource_id": item.resource_id,
                "reason": item.reason,
                "created_by": item.created_by,
                "created_at": item.created_at,
                "active": item.active,
            }
            for item in suspensions
        ],
    }


@router.post("/app/api/admin/abuse/evaluate", status_code=201, response_model=AdminAbuseEvaluationOut)
def browser_admin_abuse_evaluate(
    payload: AbuseIn,
    ctx: dict = Depends(admin_browser_context),
    _browser_session=Depends(admin_csrf_guard),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    return evaluate_abuse(payload, ctx=ctx, s=session)


@router.post("/app/api/admin/abuse/suspensions", status_code=201, response_model=AdminSuspensionCreateOut)
def browser_admin_abuse_suspend(
    payload: SuspendIn,
    ctx: dict = Depends(admin_browser_context),
    _browser_session=Depends(admin_csrf_guard),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    return suspend_resource(payload, ctx=ctx, s=session)


@router.post("/app/api/admin/abuse/suspensions/{item_id}/release", response_model=AdminSuspensionReleaseOut)
def browser_admin_abuse_release(
    item_id: str,
    ctx: dict = Depends(admin_browser_context),
    _browser_session=Depends(admin_csrf_guard),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    return release_resource(item_id, ctx=ctx, s=session)


@router.post("/app/api/admin/abuse/alerts/{alert_id}/state", response_model=AdminAlertStateOut)
def browser_admin_abuse_alert_state(
    alert_id: str,
    payload: AbuseAlertStateIn,
    ctx: dict = Depends(admin_browser_context),
    _browser_session=Depends(admin_csrf_guard),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    item = session.get(AbuseAlert, alert_id)
    if not item:
        raise HTTPException(404, "abuse_alert_not_found")
    if item.state == payload.state:
        return {"id": item.id, "state": item.state}
    if item.state == "RESOLVED":
        raise HTTPException(409, "abuse_alert_already_resolved")
    if item.state == "ACKNOWLEDGED" and payload.state != "RESOLVED":
        raise HTTPException(409, "abuse_alert_transition_invalid")
    item.state = payload.state
    audit(
        session,
        {**ctx, "tenant": item.tenant_id},
        "abuse.alert." + payload.state.lower(),
    )
    session.commit()
    return {"id": item.id, "state": item.state}


def _run_summary(item: ReconciliationRun) -> dict[str, Any]:
    details = _safe_json_list(item.details_json)
    return {
        "id": item.id,
        "tenant_id": item.tenant_id,
        "kind": item.kind,
        "state": item.state,
        "drift_count": item.drift_count,
        "detail_count": len(details),
        "started_at": item.started_at,
        "completed_at": item.completed_at,
    }


@router.get("/app/api/admin/reconciliation", response_model=AdminReconciliationListOut)
def browser_admin_reconciliation(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: dict = Depends(admin_browser_context),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    rows = session.scalars(
        select(ReconciliationRun)
        .order_by(ReconciliationRun.started_at.desc(), ReconciliationRun.id.desc())
        .limit(limit)
    ).all()
    return {"runs": [_run_summary(item) for item in rows]}


@router.get("/app/api/admin/reconciliation/billing", response_model=AdminBillingReconciliationOut)
def browser_admin_billing_reconciliation(
    tenant_id: Optional[str] = Query(default=None, min_length=1, max_length=200),
    ctx: dict = Depends(admin_browser_context),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    issues = reconcile_billing(session, tenant_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "status": "PASS" if not issues else "DRIFT",
        "issue_count": len(issues),
        "issues": [issue.as_dict() for issue in issues],
        "auto_corrected": False,
    }


@router.get("/app/api/admin/reconciliation/{run_id}", response_model=AdminReconciliationDetailOut)
def browser_admin_reconciliation_detail(
    run_id: str,
    ctx: dict = Depends(admin_browser_context),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    item = session.get(ReconciliationRun, run_id)
    if not item:
        raise HTTPException(404, "reconciliation_run_not_found")
    return {**_run_summary(item), "details": _safe_json_list(item.details_json)}


@router.post("/app/api/admin/reconciliation", status_code=201, response_model=AdminReconciliationExecutionOut)
def browser_admin_reconciliation_run(
    ctx: dict = Depends(admin_browser_context),
    _browser_session=Depends(admin_csrf_guard),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    return run_platform_reconciliation(ctx=ctx, s=session)


@router.get("/app/api/admin/billing/overview", response_model=AdminBillingOverviewOut)
def browser_admin_billing_overview(
    ctx: dict = Depends(admin_browser_context),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    drift = reconcile_billing(session)
    recent_invoices = session.scalars(
        select(Invoice).order_by(Invoice.created_at.desc(), Invoice.id.desc()).limit(25)
    ).all()
    prices = session.scalars(
        select(BillingPrice)
        .where(BillingPrice.retired_at == None)
        .order_by(BillingPrice.effective_at.desc())
        .limit(50)
    ).all()
    plans = {item.id: item for item in session.scalars(select(BillingPlan)).all()}
    return {
        "configuration": _billing_configuration(),
        "counts": {
            "subscriptions": _count_by(
                session, BillingSubscription, BillingSubscription.status
            ),
            "invoices": _count_by(session, Invoice, Invoice.status),
            "payments": _count_by(session, Payment, Payment.status),
            "refunds": _count_by(session, Refund, Refund.status),
            "work_items": _count_by(session, BillingWorkItem, BillingWorkItem.state),
        },
        "billing_drift": {
            "status": "PASS" if not drift else "DRIFT",
            "issue_count": len(drift),
        },
        "active_prices": [
            {
                "price_id": price.id,
                "plan_id": price.plan_id,
                "plan_code": plans.get(price.plan_id).code if plans.get(price.plan_id) else None,
                "plan_name": plans.get(price.plan_id).name if plans.get(price.plan_id) else None,
                "version": price.version,
                "currency": price.currency,
                "billing_cycle": price.billing_cycle,
                "base_amount": _decimal(price.base_amount),
                "included_units": price.included_units,
                "overage_amount": str(price.overage_amount),
                "effective_at": price.effective_at,
            }
            for price in prices
        ],
        "recent_invoices": [
            {
                "id": invoice.id,
                "number": invoice.number,
                "tenant_id": invoice.tenant_id,
                "tenant_name": _tenant_name(session, invoice.tenant_id),
                "status": invoice.status,
                "total": _decimal(invoice.total),
                "currency": invoice.currency,
                "due_at": invoice.due_at,
                "created_at": invoice.created_at,
            }
            for invoice in recent_invoices
        ],
    }


@router.get("/app/api/admin/billing/subscriptions", response_model=list[AdminBillingSubscriptionOut])
def browser_admin_billing_subscriptions(
    status: Optional[str] = Query(default=None, min_length=1, max_length=40),
    tenant_id: Optional[str] = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: dict = Depends(admin_browser_context),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    query = select(BillingSubscription)
    if status:
        query = query.where(BillingSubscription.status == status.upper())
    if tenant_id:
        query = query.where(BillingSubscription.tenant_id == tenant_id)
    rows = session.scalars(
        query.order_by(BillingSubscription.period_end.asc(), BillingSubscription.id.asc()).limit(limit)
    ).all()
    result = []
    for item in rows:
        plan = session.get(BillingPlan, item.plan_id)
        price = session.get(BillingPrice, item.price_id)
        open_invoices = session.scalar(
            select(func.count()).select_from(Invoice).where(
                Invoice.tenant_id == item.tenant_id,
                Invoice.status.in_(("OPEN", "PAST_DUE", "PARTIALLY_PAID")),
            )
        ) or 0
        result.append(
            {
                "id": item.id,
                "tenant_id": item.tenant_id,
                "tenant_name": _tenant_name(session, item.tenant_id),
                "status": item.status,
                "plan_code": plan.code if plan else None,
                "plan_name": plan.name if plan else None,
                "billing_cycle": price.billing_cycle if price else None,
                "currency": price.currency if price else None,
                "period_start": item.period_start,
                "period_end": item.period_end,
                "trial_end": item.trial_end,
                "cancel_at_period_end": item.cancel_at_period_end,
                "version": item.version,
                "open_invoice_count": int(open_invoices),
            }
        )
    return result


@router.post("/app/api/admin/billing/dunning", response_model=AdminDunningOut)
def browser_admin_billing_dunning(
    payload: AdminDunningIn,
    ctx: dict = Depends(admin_browser_context),
    _browser_session=Depends(admin_csrf_guard),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    if payload.suspend_days <= payload.grace_days:
        raise HTTPException(422, "billing_dunning_schedule_invalid")
    try:
        settings = load_billing_settings()
    except BillingConfigError:
        raise HTTPException(503, "billing_configuration_invalid") from None
    if not settings.enabled or not settings.dunning_enabled:
        raise HTTPException(503, "billing_dunning_disabled")
    return run_dunning(
        DunningIn(
            grace_days=payload.grace_days,
            suspend_days=payload.suspend_days,
        ),
        ctx=ctx,
        s=session,
    )


def _encode_audit_cursor(item: Audit) -> str:
    created = item.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    raw = json.dumps(
        {"created_at": created.astimezone(timezone.utc).isoformat(), "id": item.id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_audit_cursor(value: str) -> tuple[datetime, str]:
    try:
        padding = "=" * ((4 - len(value) % 4) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding))
        created = datetime.fromisoformat(str(decoded["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        item_id = str(decoded["id"])
        if not item_id:
            raise ValueError
        return created.astimezone(timezone.utc), item_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(422, "invalid_audit_cursor") from None


@router.get("/app/api/admin/audit", response_model=AdminAuditPageOut)
def browser_admin_audit(
    tenant_id: Optional[str] = Query(default=None, min_length=1, max_length=200),
    actor: Optional[str] = Query(default=None, min_length=1, max_length=200),
    action_prefix: Optional[str] = Query(default=None, min_length=1, max_length=160),
    cursor: Optional[str] = Query(default=None, min_length=1, max_length=1000),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: dict = Depends(admin_browser_context),
    session: Session = Depends(db),
):
    _require_platform_admin(ctx, session)
    query = select(Audit)
    if tenant_id:
        query = query.where(Audit.tenant_id == tenant_id)
    if actor:
        query = query.where(Audit.actor == actor)
    if action_prefix:
        query = query.where(Audit.action.startswith(action_prefix))
    if cursor:
        created_at, item_id = _decode_audit_cursor(cursor)
        query = query.where(
            or_(
                Audit.created_at < created_at,
                and_(Audit.created_at == created_at, Audit.id < item_id),
            )
        )
    rows = session.scalars(
        query.order_by(Audit.created_at.desc(), Audit.id.desc()).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "items": [
            {
                "id": item.id,
                "tenant_id": item.tenant_id,
                "tenant_name": _tenant_name(session, item.tenant_id),
                "actor": item.actor,
                "action": item.action,
                "created_at": item.created_at,
            }
            for item in page
        ],
        "next_cursor": _encode_audit_cursor(page[-1]) if has_more and page else None,
    }
