"""Short-lived encrypted credential response storage and irreversible cleanup."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from prometheus_client import Counter, Gauge
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .durable_results import seal, unseal
from .main import Base, auth, db, platform_metric


router = APIRouter(prefix="/v1/secret-responses", tags=["Credentials"])
REDACTED_TOTAL = platform_metric(Counter(
    "klyrow_secret_response_redacted_total",
    "Expired credential response documents irreversibly redacted",
    ["codestra_business", "application", "service", "environment", "server", "region", "deployment"],
))
PENDING_EXPIRY = platform_metric(Gauge(
    "klyrow_secret_response_pending_expiry",
    "Credential response documents still inside their retrieval window",
    ["codestra_business", "application", "service", "environment", "server", "region", "deployment"],
))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SecretResponse(Base):
    __tablename__ = "secret_responses"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    encrypted_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    retrieved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    redacted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


def retention_seconds() -> int:
    try:
        value = int(os.getenv("KLYROW_SECRET_RESPONSE_RETENTION_SECONDS", "86400"))
    except ValueError as exc:
        raise ValueError("invalid_secret_response_retention") from exc
    if not 60 <= value <= 86_400:
        raise ValueError("invalid_secret_response_retention")
    return value


def _binding(row: SecretResponse) -> list[str]:
    return [
        "secret-response",
        row.tenant_id,
        row.id,
        row.resource_type,
        row.resource_id,
        row.action,
    ]


def record_secret_response(
    session: Session,
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    action: str,
    payload: dict,
    actor: str,
    current: Optional[datetime] = None,
) -> SecretResponse:
    """Persist ciphertext in the caller's credential mutation transaction."""
    stamp = current or utcnow()
    item = SecretResponse(
        id="secresp_" + uuid.uuid4().hex,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        created_by=actor,
        created_at=stamp,
        expires_at=stamp + timedelta(seconds=retention_seconds()),
    )
    item.encrypted_payload = seal(payload, _binding(item))
    session.add(item)
    return item


def response_metadata(item: SecretResponse) -> dict:
    return {
        "secret_response_id": item.id,
        "secret_expires_at": item.expires_at,
    }


def read_secret_response(item: SecretResponse, *, current: Optional[datetime] = None) -> dict:
    stamp = current or utcnow()
    expires = item.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if item.encrypted_payload is None or item.redacted_at is not None or expires <= stamp:
        raise HTTPException(410, "secret_response_expired")
    return unseal(item.encrypted_payload, _binding(item))


def cleanup_secret_responses(
    session: Session,
    *,
    current: Optional[datetime] = None,
    limit: int = 100,
) -> int:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid_secret_cleanup_limit")
    stamp = current or utcnow()
    rows = list(session.scalars(select(SecretResponse).where(
        SecretResponse.encrypted_payload.is_not(None),
        SecretResponse.expires_at <= stamp,
    ).order_by(SecretResponse.expires_at, SecretResponse.id)
      .with_for_update(skip_locked=True).limit(limit)))
    for item in rows:
        item.encrypted_payload = None
        item.redacted_at = stamp
    if rows:
        REDACTED_TOTAL.inc(len(rows))
    return len(rows)


def refresh_metrics(session: Session) -> None:
    from sqlalchemy import func
    count = session.scalar(select(func.count()).select_from(SecretResponse).where(
        SecretResponse.encrypted_payload.is_not(None)
    ))
    PENDING_EXPIRY.set(int(count or 0))


@router.get("/{response_id}")
def secret_response_get(response_id: str, ctx=Depends(auth), session: Session = Depends(db)):
    item = session.scalar(select(SecretResponse).where(
        SecretResponse.id == response_id,
        SecretResponse.tenant_id == ctx["tenant"],
        SecretResponse.created_by == ctx["sub"],
    ).with_for_update())
    if item is None:
        raise HTTPException(404, "secret_response_not_found")
    try:
        payload = read_secret_response(item)
    except HTTPException as exc:
        if exc.status_code == 410 and item.encrypted_payload is not None:
            item.encrypted_payload = None
            item.redacted_at = utcnow()
            session.commit()
        raise
    item.retrieved_at = utcnow()
    session.commit()
    return {
        "id": item.id,
        "resource_type": item.resource_type,
        "resource_id": item.resource_id,
        "action": item.action,
        "expires_at": item.expires_at,
        "secret": payload,
    }
