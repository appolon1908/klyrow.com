"""Same-origin browser façade for governed communications setup and evidence.

This module deliberately reuses the product/domain/sender/deliverability
authorities. Browser reads may project provider readiness, but provider
credentials and raw provider payloads never cross this boundary.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth_bff import BrowserSession, browser_context, csrf_guard
from .main import Domain, db
from .messaging import (
    DkimKeyVersion,
    DomainClaim,
    DomainClaimIn,
    SenderIdentity,
    SenderIn,
    domain_claim,
    domain_claims,
    domain_verify,
    sender_create,
    senders,
)
from .provider import ProviderDomain
from .saas import DeliverabilitySnapshot, deliverability as run_deliverability
from .webmail_models import WebmailAccess, WebmailMailbox

router = APIRouter(tags=["Browser email setup"])
DELIVERABILITY_STALE_SECONDS = 24 * 60 * 60


def _management_required(ctx: dict) -> None:
    if ctx.get("role") not in {"OWNER", "ADMIN"}:
        raise HTTPException(403, "tenant_management_denied")


def _claim(s: Session, tenant_id: str, item_id: str) -> DomainClaim:
    item = s.scalar(select(DomainClaim).where(
        DomainClaim.id == item_id,
        DomainClaim.tenant_id == tenant_id,
    ))
    if item is None:
        raise HTTPException(404, "domain_not_found")
    return item


def _legacy_domain(s: Session, tenant_id: str, domain: str) -> Optional[Domain]:
    return s.scalar(select(Domain).where(
        Domain.tenant_id == tenant_id,
        Domain.domain == domain,
    ))


def _provider_domain(s: Session, tenant_id: str, domain: str) -> Optional[ProviderDomain]:
    return s.scalar(select(ProviderDomain).where(
        ProviderDomain.tenant_id == tenant_id,
        ProviderDomain.domain == domain,
    ))


def _latest_snapshot(s: Session, tenant_id: str, domain: str) -> Optional[DeliverabilitySnapshot]:
    legacy = _legacy_domain(s, tenant_id, domain)
    if legacy is None:
        return None
    return s.scalar(
        select(DeliverabilitySnapshot)
        .where(
            DeliverabilitySnapshot.tenant_id == tenant_id,
            DeliverabilitySnapshot.domain_id == legacy.id,
        )
        .order_by(DeliverabilitySnapshot.checked_at.desc(), DeliverabilitySnapshot.id.desc())
        .limit(1)
    )


def _snapshot_payload(item: Optional[DeliverabilitySnapshot]) -> dict:
    if item is None:
        return {
            "source": "none", "checked_at": None, "spf": None, "dkim": None,
            "dmarc": None, "mx": None, "ptr": None, "tls": None,
            "alerts": [], "stale": True,
        }
    try:
        details = json.loads(item.details_json or "{}")
    except (TypeError, ValueError):
        details = {}
    checked = item.checked_at
    comparable = checked if checked.tzinfo else checked.replace(tzinfo=timezone.utc)
    stale = (datetime.now(timezone.utc) - comparable).total_seconds() > DELIVERABILITY_STALE_SECONDS
    alerts = details.get("alerts")
    return {
        "source": "durable_snapshot", "checked_at": item.checked_at,
        "spf": item.spf, "dkim": item.dkim, "dmarc": item.dmarc,
        "mx": item.mx, "ptr": item.ptr, "tls": item.tls,
        "alerts": alerts if isinstance(alerts, list) else [], "stale": stale,
    }


def _provider_readiness(item: Optional[ProviderDomain]) -> dict:
    if item is None:
        return {"sending_enabled": False, "inbound_enabled": False, "status": "unknown"}
    return {
        "sending_enabled": bool(item.sending_enabled),
        "inbound_enabled": bool(item.inbound_enabled),
        "status": item.status,
    }


def _domain_detail_payload(s: Session, ctx: dict, item: DomainClaim) -> dict:
    keys = s.scalars(
        select(DkimKeyVersion)
        .where(
            DkimKeyVersion.tenant_id == ctx["tenant"],
            DkimKeyVersion.domain_claim_id == item.id,
        )
        .order_by(DkimKeyVersion.version.desc())
    ).all()
    return {
        "id": item.id,
        "domain": item.domain,
        "state": item.state,
        "verified_at": item.verified_at,
        "suspended_at": item.suspended_at,
        "created_at": item.created_at,
        "dkim": {
            "selector": item.dkim_selector,
            "version": item.dkim_version,
            "history": [{
                "selector": key.selector,
                "version": key.version,
                "active": key.active,
                "created_at": key.created_at,
                "retired_at": key.retired_at,
            } for key in keys],
        },
        "dns": {"return_path": item.return_path, "tracking_domain": item.tracking_domain},
        "deliverability": _snapshot_payload(_latest_snapshot(s, ctx["tenant"], item.domain)),
        "provider_readiness": _provider_readiness(_provider_domain(s, ctx["tenant"], item.domain)),
    }


def _deliverability_row(s: Session, ctx: dict, item: DomainClaim) -> dict:
    evidence = _snapshot_payload(_latest_snapshot(s, ctx["tenant"], item.domain))
    provider = _provider_readiness(_provider_domain(s, ctx["tenant"], item.domain))
    return {
        "id": item.id, "domain": item.domain, "state": item.state,
        "verified_at": item.verified_at, "suspended_at": item.suspended_at,
        **evidence,
        "alert_count": len(evidence["alerts"]),
        "sending_enabled": provider["sending_enabled"],
        "inbound_enabled": provider["inbound_enabled"],
        "provider_status": provider["status"],
    }


@router.get("/app/api/domains")
def browser_domains(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    return domain_claims(ctx=ctx, s=s)


@router.get("/app/api/domains/{item_id}")
def browser_domain_detail(item_id: str, ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    return _domain_detail_payload(s, ctx, _claim(s, ctx["tenant"], item_id))


@router.post("/app/api/domains", status_code=201)
def browser_domain_create(
    payload: DomainClaimIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    _management_required(ctx)
    return domain_claim(payload, ctx=ctx, s=s)


@router.post("/app/api/domains/{item_id}/verify")
def browser_domain_verify(
    item_id: str,
    payload: dict,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    _management_required(ctx)
    return domain_verify(item_id, payload, ctx=ctx, s=s)


@router.get("/app/api/senders")
def browser_senders(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    return senders(ctx=ctx, s=s)


@router.get("/app/api/senders/{sender_id}")
def browser_sender_detail(sender_id: str, ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    item = s.scalar(select(SenderIdentity).where(
        SenderIdentity.id == sender_id,
        SenderIdentity.tenant_id == ctx["tenant"],
    ))
    if item is None:
        raise HTTPException(404, "sender_not_found")
    claim = _claim(s, ctx["tenant"], item.domain_claim_id)
    mailbox = s.scalar(select(WebmailMailbox).where(
        WebmailMailbox.tenant_id == ctx["tenant"],
        WebmailMailbox.address == item.address,
        WebmailMailbox.status == "ACTIVE",
    ))
    grant_count = 0
    if mailbox is not None:
        grant_count = s.scalar(select(func.count()).select_from(WebmailAccess).where(
            WebmailAccess.tenant_id == ctx["tenant"],
            WebmailAccess.mailbox_id == mailbox.id,
        )) or 0
    return {
        "id": item.id,
        "address": item.address,
        "display_name": item.display_name,
        "reply_to": item.reply_to,
        "stream": item.stream,
        "status": item.status,
        "verified": item.verified,
        "domain": {"id": claim.id, "domain": claim.domain, "state": claim.state},
        "mailbox": {
            "id": mailbox.id if mailbox else None,
            "sending_enabled": bool(mailbox and mailbox.sending_enabled),
            "receiving_enabled": bool(mailbox and mailbox.receiving_enabled),
            "shared_grant_count": int(grant_count),
        },
    }


@router.post("/app/api/senders", status_code=201)
def browser_sender_create(
    payload: SenderIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    _management_required(ctx)
    return sender_create(payload, ctx=ctx, s=s)


@router.get("/app/api/deliverability")
def browser_deliverability(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    state: Optional[str] = Query(default=None, max_length=40),
    ctx: dict = Depends(browser_context),
    s: Session = Depends(db),
):
    query = select(DomainClaim).where(DomainClaim.tenant_id == ctx["tenant"])
    if state:
        query = query.where(DomainClaim.state == state.upper())
    rows = s.scalars(
        query.order_by(DomainClaim.created_at.desc(), DomainClaim.id.desc())
        .offset(offset).limit(limit + 1)
    ).all()
    return {
        "items": [_deliverability_row(s, ctx, item) for item in rows[:limit]],
        "limit": limit, "offset": offset, "has_more": len(rows) > limit,
    }


@router.get("/app/api/deliverability/domains/{item_id}")
def browser_deliverability_detail(item_id: str, ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    item = _claim(s, ctx["tenant"], item_id)
    detail = _domain_detail_payload(s, ctx, item)
    legacy = _legacy_domain(s, ctx["tenant"], item.domain)
    history = []
    if legacy is not None:
        snapshots = s.scalars(
            select(DeliverabilitySnapshot)
            .where(
                DeliverabilitySnapshot.tenant_id == ctx["tenant"],
                DeliverabilitySnapshot.domain_id == legacy.id,
            )
            .order_by(DeliverabilitySnapshot.checked_at.desc(), DeliverabilitySnapshot.id.desc())
            .limit(20)
        ).all()
        history = [_snapshot_payload(snapshot) for snapshot in snapshots]
    return {**detail, "history": history}


@router.post("/app/api/deliverability/domains/{item_id}/check")
def browser_deliverability_check(
    item_id: str,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=200),
):
    _management_required(ctx)
    if not idempotency_key:
        raise HTTPException(400, "idempotency_key_required")
    item = _claim(s, ctx["tenant"], item_id)
    legacy = _legacy_domain(s, ctx["tenant"], item.domain)
    if legacy is None or not legacy.verified:
        raise HTTPException(409, "verified_domain_required")
    result = run_deliverability(legacy.id, ctx=ctx, s=s)
    return {"domain_id": item.id, **result}
