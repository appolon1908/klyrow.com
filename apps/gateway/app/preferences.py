"""One-click unsubscribe and purpose-scoped suppression controls."""

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import DateTime, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import SECRET, Base, Suppression, auth, db

router = APIRouter(prefix="/v1", tags=["Preferences"])
key = hashlib.sha256((SECRET + ":unsubscribe:v1").encode()).digest()


class ScopedSuppression(Base):
    __tablename__ = "scoped_suppressions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    email: Mapped[str] = mapped_column(String, index=True)
    scope: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", "scope", "scope_id", name="uq_scoped_suppression"),
    )


class TokenIn(BaseModel):
    email: EmailStr
    scope: str = Field(pattern="^(TENANT_MARKETING|LIST)$")
    scope_id: str | None = Field(default=None, max_length=200)
    expires_days: int = Field(default=90, ge=1, le=365)


def one_click_unsubscribe_headers(
    tenant_id: str, email: str, expires_days: int = 90
) -> dict[str, str]:
    """Issue a tenant-scoped RFC 8058 one-click unsubscribe capability."""
    claims = {
        "aud": "klyrow-unsubscribe",
        "tenant": tenant_id,
        "email": email.lower(),
        "scope": "TENANT_MARKETING",
        "scope_id": "*",
        "jti": str(uuid.uuid4()),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(days=expires_days),
    }
    token = jwt.encode(claims, key, algorithm="HS256")
    base = os.getenv("KLYROW_PUBLIC_BASE_URL", "https://klyrow.co").rstrip("/")
    url = base + "/v1/unsubscribe?token=" + token
    return {
        "List-Unsubscribe": "<" + url + ">",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


@router.post("/unsubscribe/tokens", status_code=201)
def unsubscribe_token(x: TokenIn, ctx=Depends(auth)):
    if x.scope == "LIST" and not x.scope_id:
        raise HTTPException(422, "list_scope_id_required")
    claims = {
        "aud": "klyrow-unsubscribe",
        "tenant": ctx["tenant"],
        "email": str(x.email).lower(),
        "scope": x.scope,
        "scope_id": x.scope_id or "*",
        "jti": str(uuid.uuid4()),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(days=x.expires_days),
    }
    token = jwt.encode(claims, key, algorithm="HS256")
    return {
        "token": token,
        "url": "https://klyrow.co/v1/unsubscribe?token=" + token,
        "list_unsubscribe_post": "List-Unsubscribe=One-Click",
    }


@router.post("/unsubscribe")
def unsubscribe(token: str = Query(min_length=40), s: Session = Depends(db)):
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            audience="klyrow-unsubscribe",
            options={"require": ["exp", "iat", "jti", "tenant", "email", "scope"]},
        )
    except Exception:
        raise HTTPException(400, "invalid_or_expired_unsubscribe_token")
    tenant, email, scope = claims["tenant"], claims["email"], claims["scope"]
    if scope == "TENANT_MARKETING":
        item = s.scalar(
            select(Suppression).where(Suppression.tenant_id == tenant, Suppression.email == email)
        )
        if not item:
            item = Suppression(
                id=str(uuid.uuid4()), tenant_id=tenant, email=email, reason="unsubscribe_marketing"
            )
            s.add(item)
        elif item.reason not in {"hard_bounce", "complaint"}:
            item.reason = "unsubscribe_marketing"
    else:
        scope_id = str(claims.get("scope_id") or "")
        if not scope_id:
            raise HTTPException(400, "invalid_unsubscribe_scope")
        item = s.scalar(
            select(ScopedSuppression).where(
                ScopedSuppression.tenant_id == tenant,
                ScopedSuppression.email == email,
                ScopedSuppression.scope == "LIST",
                ScopedSuppression.scope_id == scope_id,
            )
        )
        if not item:
            s.add(
                ScopedSuppression(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant,
                    email=email,
                    scope="LIST",
                    scope_id=scope_id,
                    reason="unsubscribe",
                )
            )
    s.commit()
    return {"unsubscribed": True, "scope": scope, "email": email}


def enforce_suppression(
    s: Session, tenant_id: str, email: str, stream: str, campaign_id: str | None
):
    item = s.scalar(
        select(Suppression).where(Suppression.tenant_id == tenant_id, Suppression.email == email)
    )
    if item and (
        item.reason in {"hard_bounce", "complaint", "invalid", "abuse", "policy"}
        or stream == "marketing"
    ):
        raise HTTPException(422, "recipient_suppressed")
    if (
        stream == "marketing"
        and campaign_id
        and s.scalar(
            select(ScopedSuppression.id).where(
                ScopedSuppression.tenant_id == tenant_id,
                ScopedSuppression.email == email,
                ScopedSuppression.scope == "LIST",
                ScopedSuppression.scope_id == campaign_id,
            )
        )
    ):
        raise HTTPException(422, "recipient_suppressed")
