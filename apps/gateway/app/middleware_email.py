"""Command-bound Middleware email transport over the governed message service."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError
from sqlalchemy import DateTime, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import main as core
from .production_api import _has_permission
from .production_authorization import (
    ProductionAuthorizationError,
    validate_production_authorization,
)

router = APIRouter(tags=["Middleware email"])


class EmailCommandBinding(core.Base):
    __tablename__ = "middleware_email_command_bindings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    command_id: Mapped[str] = mapped_column(String)
    idempotency_identity: Mapped[str] = mapped_column(String, unique=True)
    request_hash: Mapped[str] = mapped_column(String)
    correlation_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class EmailCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=8, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=120)
    sender: EmailStr
    recipients: list[EmailStr] = Field(min_length=1, max_length=1)
    subject: str = Field(min_length=1, max_length=998)
    text: str | None = Field(default=None, max_length=100_000)
    html: str | None = Field(default=None, max_length=100_000)
    reply_to: EmailStr | None = None
    stream: Literal["transactional", "operational"] = "transactional"
    classification: Literal["operational-alert"] | None = None
    recipient_policy_id: str | None = None
    sender_policy_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _identity(ctx: dict, value: str, action: str) -> str:
    return core.scoped_idempotency_key(ctx, value, action=action, resource="email")


def _authorize(ctx: dict, *, read: bool = False) -> None:
    if not ctx.get("service"):
        raise HTTPException(403, "middleware_service_identity_required")
    capability = "klyrow.read" if read else "klyrow.send"
    if not _has_permission(ctx, capability):
        raise HTTPException(403, "permission_denied")
    if not core.SAFE_MODE:
        expected = os.getenv("KLYROW_MIDDLEWARE_EMAIL_IDENTITY", "").strip()
        if not expected:
            raise HTTPException(503, "middleware_email_identity_unconfigured")
        if ctx.get("sub") != expected:
            raise HTTPException(403, "middleware_email_identity_denied")


def _message_key(binding: EmailCommandBinding) -> str:
    return "middleware-email:" + binding.id


def _read(binding: EmailCommandBinding, ctx: dict, session: Session) -> dict:
    key = core.scoped_idempotency_key(
        ctx, _message_key(binding), action="message.send", resource="messages"
    )
    prior = session.scalar(select(core.Idempotency).where(
        core.Idempotency.key == key, core.Idempotency.tenant_id == ctx["tenant"]
    ))
    message = session.scalar(select(core.Message).where(
        core.Message.id == prior.resource_id, core.Message.tenant_id == ctx["tenant"]
    )) if prior else None
    if message is None:
        raise HTTPException(503, "email_command_result_unavailable")
    return {
        "command_id": binding.command_id,
        "message_id": message.id,
        "tenant_id": binding.tenant_id,
        "request_hash": binding.request_hash,
        "correlation_id": binding.correlation_id,
        "sender": message.sender,
        "recipients": [message.recipient],
        "status": message.status,
    }


@router.post(
    "/v1/email/messages", status_code=202,
    openapi_extra={"requestBody": {"content": {"application/json": {
        "schema": EmailCommand.model_json_schema()
    }}}},
)
async def submit(
    payload: dict[str, Any],
    ctx: dict = Depends(core.auth),
    session: Session = Depends(core.db),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    x_correlation_id: str = Header(alias="X-Correlation-ID", min_length=8, max_length=200),
) -> dict:
    _authorize(ctx)
    try:
        command = EmailCommand.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "unsupported_email_command_payload") from exc
    if command.tenant_id != ctx["tenant"]:
        raise HTTPException(403, "tenant_mismatch")
    if not command.text and not command.html:
        raise HTTPException(422, "email_content_required")
    if command.stream == "operational":
        if (
            command.sender != "alerts@codestra.co"
            or command.recipients != ["appolon1908@gmail.com"]
            or command.reply_to != "appolon1908@gmail.com"
            or command.classification != "operational-alert"
            or command.recipient_policy_id != "codestra-observability-admin-v1"
            or command.sender_policy_id != "codestra-alert-sender-v1"
        ):
            raise HTTPException(403, "alert_policy_mismatch")
    elif any((command.classification, command.recipient_policy_id, command.sender_policy_id)):
        raise HTTPException(422, "alert_policy_requires_operational_stream")

    production_authorization = None
    if not core.SAFE_MODE:
        try:
            production_authorization = validate_production_authorization(
                command.metadata.get("productionAuthorization"),
                tenant_id=ctx["tenant"],
                message_id=command.message_id,
                correlation_id=x_correlation_id,
                sender=str(command.sender),
                recipients=[str(value) for value in command.recipients],
                category=str(command.metadata.get("category") or "transactional"),
                idempotency_key=idempotency_key,
            )
        except ProductionAuthorizationError as exc:
            raise HTTPException(403, str(exc)) from exc

    # The tenant lock serializes duplicate command/key reservations and is held
    # until _send commits the binding, message, usage and native idempotency row.
    tenant = session.scalar(select(core.Tenant).where(
        core.Tenant.id == ctx["tenant"]
    ).with_for_update())
    if not tenant or not tenant.enabled:
        raise HTTPException(403, "tenant_suspended")
    identity = _identity(ctx, command.message_id, "middleware.email.command")
    idem = _identity(ctx, idempotency_key, "middleware.email.idempotency")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    binding = session.get(EmailCommandBinding, identity)
    reused_key = session.scalar(select(EmailCommandBinding).where(
        EmailCommandBinding.idempotency_identity == idem
    ))
    if binding or reused_key:
        if (
            binding is None or reused_key is None or binding.id != reused_key.id
            or binding.request_hash != digest or binding.correlation_id != x_correlation_id
        ):
            raise HTTPException(409, "email_command_identity_conflict")
        return _read(binding, ctx, session)

    binding = EmailCommandBinding(
        id=identity, tenant_id=ctx["tenant"], command_id=command.message_id,
        idempotency_identity=idem, request_hash=digest, correlation_id=x_correlation_id,
    )
    message = core.MailIn(
        sender=command.sender, to=command.recipients[0], reply_to=command.reply_to,
        subject=command.subject, text=command.text, html=command.html or "",
        stream="system" if command.stream == "operational" else "transactional",
        callback_metadata={"operation_id": command.message_id, "correlation_id": x_correlation_id},
    )
    try:
        session.add(binding)
        session.flush()
        await core._send(
            message,
            ctx,
            session,
            _message_key(binding),
            _production_authorization=production_authorization,
        )
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "email_command_identity_conflict") from exc
    except Exception:
        session.rollback()
        raise
    return _read(binding, ctx, session)


@router.get("/v1/email/messages/{command_id}")
def readback(
    command_id: str,
    ctx: dict = Depends(core.auth),
    session: Session = Depends(core.db),
) -> dict:
    _authorize(ctx, read=True)
    binding = session.get(EmailCommandBinding, _identity(ctx, command_id, "middleware.email.command"))
    if binding is None or binding.tenant_id != ctx["tenant"]:
        raise HTTPException(404, "not_found")
    return _read(binding, ctx, session)
