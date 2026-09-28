"""Tenant-scoped browser support center.

The browser surface reuses the canonical SupportTicket model from operations.py.
It persists customer replies locally and never queues Odoo/provider work itself.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth_bff import browser_context, csrf_guard
from .db_base import Base
from .main import audit, db
from .operations import SupportTicket

router = APIRouter(prefix="/app/api/support", tags=["Browser support"])

SupportCategory = Literal["technical", "deliverability", "billing", "account", "abuse", "domain"]
SupportPriority = Literal["LOW", "NORMAL", "HIGH", "URGENT"]


def now() -> datetime:
    return datetime.now(timezone.utc)


class SupportTicketMessage(Base):
    __tablename__ = "support_ticket_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("support_tickets.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    author_user_id: Mapped[str] = mapped_column(String, index=True)
    author_kind: Mapped[str] = mapped_column(String(20), default="CUSTOMER")
    body: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(220))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_support_message_tenant_idempotency",
        ),
    )


class SupportTicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=10000)
    category: SupportCategory = "technical"
    priority: SupportPriority = "NORMAL"


class SupportReplyCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _ticket(s: Session, ticket_id: str, tenant_id: str) -> SupportTicket:
    item = s.scalar(
        select(SupportTicket).where(
            SupportTicket.id == ticket_id,
            SupportTicket.tenant_id == tenant_id,
        )
    )
    if item is None:
        raise HTTPException(404, "support_ticket_not_found")
    return item


def _ticket_payload(s: Session, item: SupportTicket, *, include_messages: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": item.id,
        "subject": item.subject,
        "category": item.category,
        "priority": item.priority,
        "status": item.status,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "last_message_at": item.updated_at,
        "odoo_reference": item.odoo_reference,
    }
    if include_messages:
        messages = s.scalars(
            select(SupportTicketMessage)
            .where(
                SupportTicketMessage.ticket_id == item.id,
                SupportTicketMessage.tenant_id == item.tenant_id,
            )
            .order_by(SupportTicketMessage.created_at, SupportTicketMessage.id)
        ).all()
        payload["messages"] = [
            {
                "id": message.id,
                "author_kind": message.author_kind,
                "body": message.body,
                "created_at": message.created_at,
            }
            for message in messages
        ]
        if not messages and item.description:
            payload["legacy_description"] = item.description
    return payload


@router.get("/tickets")
def list_support_tickets(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: dict[str, Any] = Depends(browser_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    rows = s.scalars(
        select(SupportTicket)
        .where(SupportTicket.tenant_id == ctx["tenant"])
        .order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return {
        "items": [_ticket_payload(s, item) for item in rows[:limit]],
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
    }


@router.post("/tickets", status_code=201)
def create_support_ticket(
    payload: SupportTicketCreate,
    ctx: dict[str, Any] = Depends(browser_context),
    _csrf: Any = Depends(csrf_guard),
    s: Session = Depends(db),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
) -> dict[str, Any]:
    normalized = {
        "subject": payload.subject.strip(),
        "body": payload.body.strip(),
        "category": payload.category,
        "priority": payload.priority,
    }
    if len(normalized["subject"]) < 3 or len(normalized["body"]) < 3:
        raise HTTPException(422, "support_ticket_content_required")
    request_hash = _canonical_hash(normalized)
    storage_key = "create:" + idempotency_key
    prior_message = s.scalar(
        select(SupportTicketMessage).where(
            SupportTicketMessage.tenant_id == ctx["tenant"],
            SupportTicketMessage.idempotency_key == storage_key,
        )
    )
    if prior_message is not None:
        if prior_message.request_hash != request_hash:
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        existing = _ticket(s, prior_message.ticket_id, ctx["tenant"])
        return {**_ticket_payload(s, existing, include_messages=True), "duplicate": True}

    timestamp = now()
    item = SupportTicket(
        id=str(uuid.uuid4()),
        tenant_id=ctx["tenant"],
        created_by=ctx["sub"],
        category=payload.category,
        subject=normalized["subject"],
        description=normalized["body"],
        status="OPEN",
        priority=payload.priority,
        created_at=timestamp,
        updated_at=timestamp,
    )
    first_message = SupportTicketMessage(
        id=str(uuid.uuid4()),
        ticket_id=item.id,
        tenant_id=ctx["tenant"],
        author_user_id=ctx["sub"],
        author_kind="CUSTOMER",
        body=normalized["body"],
        idempotency_key=storage_key,
        request_hash=request_hash,
        created_at=timestamp,
    )
    s.add_all([item, first_message])
    audit(s, ctx, "support.ticket.created.browser")
    s.commit()
    return {**_ticket_payload(s, item, include_messages=True), "duplicate": False}


@router.get("/tickets/{ticket_id}")
def get_support_ticket(
    ticket_id: str,
    ctx: dict[str, Any] = Depends(browser_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    return _ticket_payload(s, _ticket(s, ticket_id, ctx["tenant"]), include_messages=True)


@router.post("/tickets/{ticket_id}/messages", status_code=201)
def reply_to_support_ticket(
    ticket_id: str,
    payload: SupportReplyCreate,
    ctx: dict[str, Any] = Depends(browser_context),
    _csrf: Any = Depends(csrf_guard),
    s: Session = Depends(db),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
) -> dict[str, Any]:
    body = payload.body.strip()
    if not body:
        raise HTTPException(422, "support_reply_body_required")
    ticket = _ticket(s, ticket_id, ctx["tenant"])
    if ticket.status == "CLOSED":
        raise HTTPException(409, "support_ticket_closed")
    request_hash = _canonical_hash({"body": body})
    storage_key = "reply:" + idempotency_key
    existing = s.scalar(
        select(SupportTicketMessage).where(
            SupportTicketMessage.tenant_id == ctx["tenant"],
            SupportTicketMessage.idempotency_key == storage_key,
        )
    )
    if existing is not None:
        if existing.ticket_id != ticket.id or existing.request_hash != request_hash:
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        return {**_ticket_payload(s, ticket, include_messages=True), "duplicate": True}

    timestamp = now()
    message = SupportTicketMessage(
        id=str(uuid.uuid4()),
        ticket_id=ticket.id,
        tenant_id=ctx["tenant"],
        author_user_id=ctx["sub"],
        author_kind="CUSTOMER",
        body=body,
        idempotency_key=storage_key,
        request_hash=request_hash,
        created_at=timestamp,
    )
    s.add(message)
    if ticket.status == "RESOLVED":
        ticket.status = "OPEN"
    ticket.updated_at = timestamp
    audit(s, ctx, "support.ticket.replied.browser")
    s.commit()
    return {**_ticket_payload(s, ticket, include_messages=True), "duplicate": False}
