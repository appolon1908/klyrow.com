"""Same-origin browser BFF for tenant profiles and suppressions."""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth_bff import csrf_guard
from .capabilities import require_permission
from .main import Suppression, db
from .saas import Consent, CustomerEvent, Preference, Profile, attrs, profile_payload

router = APIRouter(prefix="/app/api", tags=["Browser profiles and suppressions"])


def browser_read_context(request: Request, s: Session = Depends(db)) -> dict[str, Any]:
    from .auth_bff import browser_context

    return browser_context(request=request, s=s)


def require_browser_permission(ctx: dict[str, Any], permission: str) -> dict[str, Any]:
    require_permission(ctx, permission)
    return ctx


def tenant_profile(s: Session, profile_id: str, tenant_id: str) -> Profile:
    profile = s.scalar(select(Profile).where(Profile.id == profile_id, Profile.tenant_id == tenant_id))
    if profile is None:
        raise HTTPException(404, "profile_not_found")
    return profile


def profile_detail(s: Session, profile: Profile) -> dict[str, Any]:
    payload = profile_payload(profile)
    payload["timeline"] = [
        {
            "id": event.id,
            "name": event.name,
            "source": event.source,
            "properties": json.loads(event.properties_json or "{}"),
            "occurred_at": event.occurred_at,
        }
        for event in s.scalars(
            select(CustomerEvent)
            .where(CustomerEvent.profile_id == profile.id, CustomerEvent.tenant_id == profile.tenant_id)
            .order_by(CustomerEvent.occurred_at.desc())
            .limit(100)
        ).all()
    ]
    payload["consent"] = [
        {
            "topic": item.topic,
            "status": item.status,
            "source": item.source,
            "version": item.version,
            "occurred_at": item.occurred_at,
        }
        for item in s.scalars(
            select(Consent).where(Consent.profile_id == profile.id, Consent.tenant_id == profile.tenant_id)
        ).all()
    ]
    payload["preferences"] = [
        {"topic": item.topic, "subscribed": item.subscribed, "updated_at": item.updated_at}
        for item in s.scalars(
            select(Preference).where(Preference.profile_id == profile.id, Preference.tenant_id == profile.tenant_id)
        ).all()
    ]
    return payload


class SuppressionIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    reason: str = Field(min_length=1, max_length=100)


@router.get("/profiles")
def profiles(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None, max_length=512),
    ctx: dict[str, Any] = Depends(browser_read_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    require_browser_permission(ctx, "contact.manage")
    from .saas import decode_profile_cursor, encode_profile_cursor
    query = select(Profile).where(Profile.tenant_id == ctx["tenant"]).order_by(Profile.created_at.desc(), Profile.id.desc())
    if cursor:
        created, profile_id = decode_profile_cursor(cursor)
        query = query.where(
            (Profile.created_at < created)
            | ((Profile.created_at == created) & (Profile.id < profile_id))
        )
    rows = s.scalars(query.limit(limit + 1)).all()
    has_next = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [profile_payload(item) for item in rows],
        "next_cursor": encode_profile_cursor(rows[-1]) if has_next and rows else None,
    }


@router.get("/profiles/{profile_id}")
def profile(
    profile_id: str,
    ctx: dict[str, Any] = Depends(browser_read_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    require_browser_permission(ctx, "contact.manage")
    return profile_detail(s, tenant_profile(s, profile_id, ctx["tenant"]))


@router.get("/suppressions")
def suppressions(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, Any] = Depends(browser_read_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    require_browser_permission(ctx, "mail.read")
    rows = s.scalars(
        select(Suppression)
        .where(Suppression.tenant_id == ctx["tenant"])
        .order_by(Suppression.email, Suppression.id)
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return {
        "items": [
            {"id": item.id, "email": item.email, "reason": item.reason}
            for item in rows[:limit]
        ],
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
    }


@router.post("/suppressions", status_code=201)
def add_suppression(
    payload: SuppressionIn,
    ctx: dict[str, Any] = Depends(browser_read_context),
    _session: Any = Depends(csrf_guard),
    s: Session = Depends(db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=200),
) -> dict[str, Any]:
    require_browser_permission(ctx, "contact.manage")
    email = payload.email.strip().lower()
    existing = s.scalar(select(Suppression).where(
        Suppression.tenant_id == ctx["tenant"], Suppression.email == email,
    ))
    if existing:
        if existing.reason != payload.reason:
            raise HTTPException(409, "suppression_reason_conflict")
        return {"id": existing.id, "email": existing.email, "reason": existing.reason, "duplicate": True}
    item = Suppression(
        id=str(uuid.uuid4()), tenant_id=ctx["tenant"], email=email, reason=payload.reason,
    )
    s.add(item)
    s.commit()
    return {"id": item.id, "email": item.email, "reason": item.reason, "duplicate": False}


@router.delete("/suppressions/{suppression_id}", status_code=204)
def remove_suppression(
    suppression_id: str,
    ctx: dict[str, Any] = Depends(browser_read_context),
    _session: Any = Depends(csrf_guard),
    s: Session = Depends(db),
) -> None:
    require_browser_permission(ctx, "contact.manage")
    item = s.scalar(select(Suppression).where(
        Suppression.id == suppression_id, Suppression.tenant_id == ctx["tenant"],
    ))
    if item is None:
        raise HTTPException(404, "suppression_not_found")
    s.delete(item)
    s.commit()
