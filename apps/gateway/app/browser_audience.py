"""Same-origin browser façade for audience profiles and suppressions."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth_bff import BrowserSession, browser_context, csrf_guard
from .capabilities import require_permission
from .main import Suppression, audit, db
from .saas import Consent, CustomerEvent, Preference, Profile, profile_payload

router = APIRouter(tags=["Browser audience"])


class BrowserSuppressionIn(BaseModel):
    email: EmailStr
    reason: str = Field(min_length=1, max_length=200)


def _profile(s: Session, tenant_id: str, profile_id: str) -> Profile:
    item = s.scalar(
        select(Profile).where(Profile.id == profile_id, Profile.tenant_id == tenant_id)
    )
    if item is None:
        raise HTTPException(404, "profile_not_found")
    return item


def _suppression_payload(item: Suppression) -> dict:
    return {"id": item.id, "email": item.email, "reason": item.reason}


@router.get("/app/api/profiles")
def browser_profiles(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: dict = Depends(browser_context),
    s: Session = Depends(db),
):
    require_permission(ctx, "contact.manage")
    rows = s.scalars(
        select(Profile)
        .where(Profile.tenant_id == ctx["tenant"])
        .order_by(Profile.created_at.desc(), Profile.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": [profile_payload(item) for item in rows], "limit": limit, "offset": offset}


@router.get("/app/api/profiles/{profile_id}")
def browser_profile_detail(
    profile_id: str,
    ctx: dict = Depends(browser_context),
    s: Session = Depends(db),
):
    require_permission(ctx, "contact.manage")
    profile = _profile(s, ctx["tenant"], profile_id)
    events = s.scalars(
        select(CustomerEvent)
        .where(CustomerEvent.tenant_id == ctx["tenant"], CustomerEvent.profile_id == profile.id)
        .order_by(CustomerEvent.occurred_at.desc())
    ).all()
    consents = s.scalars(
        select(Consent)
        .where(Consent.tenant_id == ctx["tenant"], Consent.profile_id == profile.id)
        .order_by(Consent.occurred_at.desc())
    ).all()
    preferences = s.scalars(
        select(Preference)
        .where(Preference.tenant_id == ctx["tenant"], Preference.profile_id == profile.id)
        .order_by(Preference.topic)
    ).all()
    return {
        "profile": profile_payload(profile),
        "timeline": events,
        "consents": [
            {
                "id": item.id,
                "topic": item.topic,
                "status": item.status,
                "source": item.source,
                "version": item.version,
                "occurred_at": item.occurred_at,
                "proof": json.loads(item.proof_json or "{}"),
            }
            for item in consents
        ],
        "preferences": preferences,
    }


@router.get("/app/api/suppressions")
def browser_suppressions(
    ctx: dict = Depends(browser_context), s: Session = Depends(db)
):
    require_permission(ctx, "mail.read")
    rows = s.scalars(
        select(Suppression)
        .where(Suppression.tenant_id == ctx["tenant"])
        .order_by(Suppression.email, Suppression.id)
    ).all()
    return {"items": [_suppression_payload(item) for item in rows]}


@router.post("/app/api/suppressions", status_code=201)
def browser_suppression_create(
    body: BrowserSuppressionIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    require_permission(ctx, "contact.manage")
    email = str(body.email).lower()
    item = s.scalar(
        select(Suppression).where(
            Suppression.tenant_id == ctx["tenant"], Suppression.email == email
        )
    )
    if item is None:
        item = Suppression(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], email=email, reason=body.reason)
    else:
        item.reason = body.reason
    s.add(item)
    audit(s, ctx, "suppression.upserted")
    s.commit()
    return _suppression_payload(item)


@router.delete("/app/api/suppressions/{suppression_id}", status_code=204)
def browser_suppression_delete(
    suppression_id: str,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    require_permission(ctx, "contact.manage")
    item = s.scalar(
        select(Suppression).where(
            Suppression.id == suppression_id, Suppression.tenant_id == ctx["tenant"]
        )
    )
    if item is None:
        raise HTTPException(404, "suppression_not_found")
    s.delete(item)
    audit(s, ctx, "suppression.deleted")
    s.commit()
