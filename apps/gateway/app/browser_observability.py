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
from .tenancy_onboarding import browser_context

router = APIRouter(tags=["Browser observability"])

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
