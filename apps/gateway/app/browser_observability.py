"""Platform-admin Webmail/Postal observability browser projection.

This is a read-only local projection. It never calls Postal, Prometheus, Grafana,
Odoo, or another business system directly. Cross-system commands remain owned by
Caddy -> Kong -> Middleware and approved adapters.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .main import EmailOutbox, Message, User, db
from .provider import ProviderEvent, ProviderInbound, ProviderMessage
from .platform_owner import platform_owner_role_stability_guard
from .tenancy_onboarding import browser_context

router = APIRouter(tags=["Browser observability"], dependencies=[Depends(platform_owner_role_stability_guard)])

def _count(session: Session, model, *where) -> int:
    query = select(func.count()).select_from(model)
    if where:
        query = query.where(*where)
    return int(session.scalar(query) or 0)

@router.get("/app/api/admin/observability/webmail-postal")
def webmail_postal_observability(ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    user = session.get(User, ctx["sub"])
    if not user or user.role != "platform_admin":
        raise HTTPException(403, "platform_admin_required")

    active_states = ("pending", "sending", "retry")
    active = list(session.scalars(select(EmailOutbox).where(EmailOutbox.state.in_(active_states))).all())
    now = datetime.now(timezone.utc)
    oldest_seconds = 0
    if active:
        created = min(item.created_at for item in active)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        oldest_seconds = max(0, int((now - created).total_seconds()))

    inbound_accepted = _count(session, ProviderInbound, ProviderInbound.disposition == "ACCEPT")
    inbound_quarantined = _count(session, ProviderInbound, ProviderInbound.disposition == "QUARANTINE")
    inbound_rejected = _count(session, ProviderInbound, ProviderInbound.disposition == "REJECT")
    inbound_total = inbound_accepted + inbound_quarantined + inbound_rejected

    send_failed = _count(session, EmailOutbox, EmailOutbox.state == "failed")
    send_total = _count(session, EmailOutbox)
    delivered = _count(session, ProviderMessage, ProviderMessage.status == "DELIVERED")
    bounced = _count(session, ProviderMessage, ProviderMessage.status.in_(("BOUNCED_SOFT", "BOUNCED_HARD")))
    complained = _count(session, ProviderMessage, ProviderMessage.status == "COMPLAINED")
    provider_total = _count(session, ProviderMessage)

    retry = _count(session, ProviderEvent, ProviderEvent.state == "RETRY")
    dead_letter = _count(session, ProviderEvent, ProviderEvent.state == "DEAD_LETTER")
    indeterminate = _count(session, ProviderMessage, ProviderMessage.status == "INDETERMINATE")

    return {
        "generated_at": now.isoformat(),
        "architecture": "Public/browser -> Caddy -> Kong -> Middleware -> approved Klyrow/Postal adapter; no direct cross-system business writes.",
        "inbound": {"accepted": inbound_accepted, "quarantined": inbound_quarantined, "rejected": inbound_rejected},
        "outbound": {"active": len(active), "failed": send_failed, "oldest_seconds": oldest_seconds},
        "provider": {"delivered": delivered, "bounced": bounced, "complained": complained, "indeterminate": indeterminate},
        "reconciliation": {"retry": retry, "dead_letter": dead_letter, "indeterminate": indeterminate},
        "health": {
            "inbound": "attention" if ((inbound_quarantined + inbound_rejected) / max(inbound_total, 1)) > 0.02 else "healthy",
            "outbound": "attention" if (send_failed / max(send_total, 1)) > 0.02 else "healthy",
            "queue": "critical" if oldest_seconds > 300 else ("attention" if oldest_seconds > 120 else "healthy"),
            "reconciliation": "critical" if dead_letter > 0 else ("attention" if (retry + indeterminate) > 0 else "healthy"),
        },
        "thresholds": {"inbound_failure_ratio": 0.02, "send_failure_ratio": 0.02, "queue_age_seconds": 300, "bounce_ratio": 0.05, "complaint_ratio": 0.002},
        "slo": {
            "inbound_failure_ratio": (inbound_quarantined + inbound_rejected) / max(inbound_total, 1),
            "send_failure_ratio": send_failed / max(send_total, 1),
            "bounce_ratio": bounced / max(provider_total, 1),
            "complaint_ratio": complained / max(provider_total, 1),
        },
    }


@router.get("/app/api/admin/observability/webmail-postal/slo")
def webmail_postal_slo(ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    snapshot = webmail_postal_observability(ctx, session)
    return {
        "generated_at": snapshot["generated_at"],
        "health": snapshot["health"],
        "thresholds": snapshot["thresholds"],
        "slo": snapshot["slo"],
        "queue_age_seconds": snapshot["outbound"]["oldest_seconds"],
    }

@router.get("/app/api/admin/observability/webmail-postal/incidents")
def webmail_postal_incidents(ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    snapshot = webmail_postal_observability(ctx, session)
    incidents = []
    def add(code: str, severity: str, area: str, reason: str, safe_action: str):
        incidents.append({"code": code, "severity": severity, "area": area, "reason": reason, "safe_action": safe_action})
    if snapshot["health"]["queue"] != "healthy":
        add("email_queue_age", snapshot["health"]["queue"], "Middleware -> Postal adapter", f'Oldest active item is {snapshot["outbound"]["oldest_seconds"]}s.', "Inspect durable outbox and reconciliation before retry.")
    if snapshot["health"]["inbound"] != "healthy":
        add("inbound_failure_ratio", snapshot["health"]["inbound"], "Postal -> Middleware -> Klyrow inbound adapter", f'Inbound failure ratio is {snapshot["slo"]["inbound_failure_ratio"]:.4f}.', "Verify route/domain state and authenticated provider evidence.")
    if snapshot["health"]["outbound"] != "healthy":
        add("send_failure_ratio", snapshot["health"]["outbound"], "Middleware -> Postal adapter", f'Send failure ratio is {snapshot["slo"]["send_failure_ratio"]:.4f}.', "Inspect provider outcome; reconcile indeterminate work before retry.")
    if snapshot["health"]["reconciliation"] != "healthy":
        add("reconciliation_unresolved", snapshot["health"]["reconciliation"], "Middleware reconciliation", f'{snapshot["reconciliation"]["retry"] + snapshot["reconciliation"]["dead_letter"] + snapshot["reconciliation"]["indeterminate"]} unresolved items.', "Resolve authoritative provider state before replay.")
    return {"generated_at": snapshot["generated_at"], "count": len(incidents), "items": incidents}

@router.get("/app/api/admin/observability/webmail-postal/architecture")
def webmail_postal_architecture(ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    snapshot = webmail_postal_observability(ctx, session)
    return {
        "path": ["Caddy", "Kong", "Middleware", "authorized-adapter", "Klyrow/Postal"],
        "authority": "Middleware",
        "browser_api_mode": "read-only",
        "direct_cross_system_writes": False,
        "description": snapshot["architecture"],
    }


@router.get("/app/api/admin/observability/webmail-postal/traces/{correlation_id}")
def webmail_postal_trace(correlation_id: str, ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    user = session.get(User, ctx["sub"])
    if not user or user.role != "platform_admin":
        raise HTTPException(403, "platform_admin_required")
    if not correlation_id or len(correlation_id) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for ch in correlation_id):
        raise HTTPException(422, "invalid_correlation_id")

    outbox = list(session.scalars(select(EmailOutbox).where(EmailOutbox.correlation_id == correlation_id).order_by(EmailOutbox.created_at)).all())
    provider_messages = list(session.scalars(select(ProviderMessage).where(ProviderMessage.correlation_id == correlation_id).order_by(ProviderMessage.created_at)).all())
    message_ids = {item.message_id for item in outbox} | {item.id for item in provider_messages}
    events = list(session.scalars(select(ProviderEvent).where(ProviderEvent.message_id.in_(message_ids)).order_by(ProviderEvent.created_at)).all()) if message_ids else []

    timeline = []
    for item in outbox:
        timeline.append({"at": item.created_at.isoformat(), "layer": "middleware-command", "state": item.state, "kind": "email-outbox", "attempts": item.attempts})
    for item in provider_messages:
        timeline.append({"at": item.created_at.isoformat(), "layer": "postal-adapter", "state": item.status, "kind": "provider-message", "attempts": item.attempts})
    for item in events:
        timeline.append({"at": item.created_at.isoformat(), "layer": "provider-evidence", "state": item.state, "kind": item.kind, "attempts": item.attempts})
    timeline.sort(key=lambda item: item["at"])

    return {
        "correlation_id": correlation_id,
        "path": ["Caddy", "Kong", "Middleware", "authorized-adapter", "Klyrow/Postal"],
        "found": bool(timeline),
        "timeline": timeline,
        "privacy": "No addresses, subjects, bodies, tokens, provider IDs or payloads are returned.",
        "direct_cross_system_writes": False,
    }


def _platform_admin(ctx: dict, session: Session) -> User:
    user = session.get(User, ctx["sub"])
    if not user or user.role != "platform_admin":
        raise HTTPException(403, "platform_admin_required")
    return user

@router.get("/app/api/admin/observability/users")
def user_suite_observability(ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    _platform_admin(ctx, session)
    from .auth_bff import BrowserSession
    total = _count(session, User)
    enabled = _count(session, User, User.enabled == True)
    disabled = max(total - enabled, 0)
    sessions_active = _count(session, BrowserSession, BrowserSession.revoked_at.is_(None))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite": "users",
        "users": {"total": total, "enabled": enabled, "disabled": disabled},
        "sessions": {"active": sessions_active},
        "health": "attention" if disabled > 0 else "healthy",
        "authority": "Keycloak/OIDC identity with Klyrow browser-session projection",
        "cross_system_path": ["Caddy", "Kong", "Middleware", "identity-adapter", "Keycloak/Klyrow"],
        "direct_cross_system_writes": False,
    }

@router.get("/app/api/admin/observability/billing")
def billing_suite_observability(ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    _platform_admin(ctx, session)
    from .billing import Invoice, Payment, Refund
    from .payment_attempts import PaymentAttempt
    attempts = _count(session, PaymentAttempt)
    captured = _count(session, PaymentAttempt, PaymentAttempt.state == "CAPTURED")
    failed = _count(session, PaymentAttempt, PaymentAttempt.state.in_(("FAILED", "CANCELED", "EXPIRED")))
    invoices = _count(session, Invoice)
    payments = _count(session, Payment)
    refunds = _count(session, Refund)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite": "billing",
        "payment_attempts": {"total": attempts, "captured": captured, "failed": failed},
        "ledger": {"invoices": invoices, "payments": payments, "refunds": refunds},
        "health": "attention" if failed > 0 else "healthy",
        "authority": "Klyrow billing ledger; provider effects only through Middleware billing adapters",
        "cross_system_path": ["Caddy", "Kong", "Middleware", "billing-adapter", "Klyrow/provider"],
        "direct_cross_system_writes": False,
        "sensitive_payment_data_returned": False,
    }

@router.get("/app/api/admin/observability/system")
def admin_system_observability(ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    _platform_admin(ctx, session)
    from .main import MiddlewareCommandOperation
    from .operations import IntegrationOutbox
    commands = _count(session, MiddlewareCommandOperation)
    command_failed = _count(session, MiddlewareCommandOperation, MiddlewareCommandOperation.state == "failed")
    integration_pending = _count(session, IntegrationOutbox, IntegrationOutbox.state.in_(("PENDING", "RETRY")))
    integration_dead = _count(session, IntegrationOutbox, IntegrationOutbox.state == "DEAD_LETTER")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite": "admin-system",
        "middleware_commands": {"total": commands, "failed": command_failed},
        "integration_outbox": {"pending_or_retry": integration_pending, "dead_letter": integration_dead},
        "health": "critical" if integration_dead else ("attention" if command_failed or integration_pending else "healthy"),
        "authority": "Middleware command kernel",
        "cross_system_path": ["Caddy", "Kong", "Middleware", "authorized-adapter"],
        "direct_cross_system_writes": False,
    }

@router.get("/app/api/admin/observability/operations-center")
def operations_center(ctx: dict = Depends(browser_context), session: Session = Depends(db)):
    _platform_admin(ctx, session)
    users = user_suite_observability(ctx, session)
    mail = webmail_postal_observability(ctx, session)
    billing = billing_suite_observability(ctx, session)
    system = admin_system_observability(ctx, session)
    severity = {"healthy": 0, "attention": 1, "critical": 2}
    mail_health = max(mail["health"].values(), key=lambda value: severity[value])
    suites = {
        "users": users["health"],
        "email": mail_health,
        "billing": billing["health"],
        "system": system["health"],
    }
    overall = max(suites.values(), key=lambda value: severity[value])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_health": overall,
        "suites": suites,
        "architecture": ["Caddy", "Kong", "Middleware", "authorized-adapter", "service/provider"],
        "questions_answered": [
            "Are users and sessions operational?",
            "Is email inbound/outbound healthy?",
            "Is billing processing healthy?",
            "Are Middleware commands/integrations healthy?",
            "Which suite requires operator attention first?",
        ],
        "direct_cross_system_writes": False,
    }
