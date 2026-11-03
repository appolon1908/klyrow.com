"""Service-authenticated Klyrow sender-identity provisioning for Middleware.

Mirrors middleware_email.py's service-auth pattern: Middleware's existing
static KLYROW_MIDDLEWARE_API_KEY resolves to ctx["service"]=True with
role=tenant_admin, which already satisfies capabilities.has_permission()
unconditionally for any permission string. This adds sender-identity
provisioning (create/read/disable) as a machine-callable sibling of the
existing human-facing POST /v1/senders (messaging.sender_create), which
requires an interactive tenant-member session and is not reachable with a
bare service credential. Reuses the same SenderIdentity/DomainClaim models,
validation rules, and AllowedSender bookkeeping as that route rather than
introducing a parallel identity concept.
"""

from __future__ import annotations

import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .main import AllowedSender, audit, auth, db, rate_buckets
from .messaging import STREAMS, DomainClaim, SenderIdentity

router = APIRouter(tags=["Middleware sender identities"])


def _authorize(ctx: dict) -> None:
    if not ctx.get("service"):
        raise HTTPException(403, "middleware_service_identity_required")


def _sender_identity_rate_limit(ctx: dict, action: str) -> None:
    """Same bucket/limiter shape as main.auth_rate, keyed by tenant+action.

    This route is service-authenticated (a single Middleware caller, not a
    browser), so the key is the tenant rather than a client IP - bounding
    how fast any one tenant's sender-identity data can be enumerated or
    churned, independent of Middleware's own per-caller rate limit on the
    same operations (defense in depth, not a replacement for it).
    """
    identity = ctx.get("tenant", "unknown")
    now = time.time()
    bucket = rate_buckets[("sender-identity", action, identity)]
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    limit = int(os.getenv("KLYROW_SENDER_IDENTITY_RATE_PER_MINUTE", "30"))
    if len(bucket) >= limit:
        raise HTTPException(429, "rate_limit_exceeded")
    bucket.append(now)


class SenderIdentityIn(BaseModel):
    domain_claim_id: str = Field(min_length=1, max_length=200)
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    reply_to: EmailStr | None = None
    stream: str = Field(default="transactional")


def _serialize(item: SenderIdentity) -> dict:
    return {
        "id": item.id,
        "tenant_id": item.tenant_id,
        "domain_claim_id": item.domain_claim_id,
        "address": item.address,
        "status": item.status,
        "verified": item.verified,
    }


@router.post("/v1/internal/sender-identities", status_code=201)
def create_sender_identity(
    body: SenderIdentityIn,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> dict:
    _authorize(ctx)
    _sender_identity_rate_limit(ctx, "create")
    claim = s.get(DomainClaim, body.domain_claim_id)
    if not claim or claim.tenant_id != ctx["tenant"]:
        raise HTTPException(404, "domain_claim_not_found")
    if claim.state not in {"VERIFIED", "SENDING_ENABLED"}:
        raise HTTPException(409, "verified_domain_required")
    address = body.email.lower()
    if address.rsplit("@", 1)[1] != claim.domain:
        raise HTTPException(403, "sender_spoofing_denied")
    kind = body.stream.upper()
    if kind not in STREAMS:
        raise HTTPException(422, "invalid_message_stream")

    # Idempotent: a repeat provisioning call for the same tenant+address
    # returns the existing identity rather than colliding on the unique
    # constraint or creating a duplicate.
    existing = s.scalar(
        select(SenderIdentity).where(
            SenderIdentity.tenant_id == ctx["tenant"],
            SenderIdentity.address == address,
        )
    )
    if existing:
        return _serialize(existing)

    item = SenderIdentity(
        id=str(uuid.uuid4()),
        tenant_id=ctx["tenant"],
        domain_claim_id=claim.id,
        address=address,
        display_name=body.display_name,
        reply_to=str(body.reply_to).lower() if body.reply_to else None,
        stream=kind,
        status="ACTIVE",
        verified=True,
    )
    s.add(item)
    allowed = s.scalar(
        select(AllowedSender).where(
            AllowedSender.tenant_id == ctx["tenant"],
            AllowedSender.address == address,
        )
    )
    if allowed is None:
        s.add(
            AllowedSender(
                id=str(uuid.uuid4()),
                tenant_id=ctx["tenant"],
                address=address,
                role="agent_sender",
                enabled=True,
            )
        )
    else:
        allowed.enabled = True
    audit(s, ctx, "sender_identity.provisioned")
    s.commit()
    return _serialize(item)


@router.get("/v1/internal/sender-identities/{item_id}")
def get_sender_identity(item_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)) -> dict:
    _authorize(ctx)
    _sender_identity_rate_limit(ctx, "read")
    item = s.get(SenderIdentity, item_id)
    if not item or item.tenant_id != ctx["tenant"]:
        raise HTTPException(404, "not_found")
    audit(s, ctx, "sender_identity.read")
    s.commit()
    return _serialize(item)


@router.post("/v1/internal/sender-identities/{item_id}/disable")
def disable_sender_identity(
    item_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict:
    _authorize(ctx)
    _sender_identity_rate_limit(ctx, "disable")
    item = s.get(SenderIdentity, item_id)
    if not item or item.tenant_id != ctx["tenant"]:
        raise HTTPException(404, "not_found")
    item.status = "DISABLED"
    allowed = s.scalar(
        select(AllowedSender).where(
            AllowedSender.tenant_id == ctx["tenant"],
            AllowedSender.address == item.address,
        )
    )
    if allowed:
        allowed.enabled = False
    audit(s, ctx, "sender_identity.disabled")
    s.commit()
    return _serialize(item)
