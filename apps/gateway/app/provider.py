import hashlib
import json
import os
import uuid
import base64
import secrets
import asyncio
import time
import dns.resolver
from email import policy as mime_policy
from email.parser import BytesParser
from pathlib import PurePath
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .guards import authorize_send, enforce_consent, suppression_record
from .guards import enforce_suppression
from .capabilities import require_permission
from .main import AllowedSender, Audit, Base, Domain, InboundRouteConfig, Message, Tenant, auth, db, verify_postal_signature

router = APIRouter(prefix="/v1/internal/email", tags=["Klyrow provider"])
status_router = APIRouter(tags=["Klyrow provider status"])
now = lambda: datetime.now(timezone.utc)
smtp_hasher = PasswordHasher()

DOMAIN_STATES = {"PENDING", "DNS_REQUIRED", "VERIFYING", "VERIFIED", "SENDING_ENABLED", "SUSPENDED", "REMOVED"}
SENDER_STATES = {"PENDING", "ACTIVE", "SUSPENDED", "REMOVED"}
STREAMS = {"TRANSACTIONAL", "SECURITY", "SYSTEM", "MARKETING", "BULK"}
MESSAGE_STATES = {"CREATED", "QUEUED", "PROCESSING", "SUBMITTED", "SENT", "DELIVERED", "DEFERRED", "INDETERMINATE", "BOUNCED_SOFT", "BOUNCED_HARD", "COMPLAINED", "SUPPRESSED", "FAILED", "DEAD_LETTER"}
REPUTATION_STATES = {"GOOD", "WATCH", "LIMITED", "SUSPENDED"}


class ProviderDomain(Base):
    __tablename__ = "provider_domains"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    domain: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, default="PENDING", index=True)
    ownership_token: Mapped[str] = mapped_column(String)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    dkim_selector: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    dkim_key_version: Mapped[int] = mapped_column(Integer, default=1)
    sending_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    inbound_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SenderIdentity(Base):
    __tablename__ = "sender_identities"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_provider_sender_tenant_email"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    domain_id: Mapped[str] = mapped_column(ForeignKey("provider_domains.id"), index=True)
    email: Mapped[str] = mapped_column(String, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reply_to: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stream: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="PENDING", index=True)


class TenantMailPolicy(Base):
    __tablename__ = "tenant_mail_policies"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    sending_disabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sandbox_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=1000)
    hourly_limit: Mapped[int] = mapped_column(Integer, default=100)
    max_message_bytes: Mapped[int] = mapped_column(Integer, default=10_000_000)
    max_attachment_bytes: Mapped[int] = mapped_column(Integer, default=5_000_000)
    allowed_test_recipients_json: Mapped[str] = mapped_column(Text, default="[]")
    reputation_state: Mapped[str] = mapped_column(String, default="GOOD")
    warmup_daily_limit: Mapped[int] = mapped_column(Integer, default=100)
    warmup_hourly_limit: Mapped[int] = mapped_column(Integer, default=20)
    warmup_growth_percent: Mapped[int] = mapped_column(Integer, default=20)
    ip_pool: Mapped[str] = mapped_column(String, default="SHARED")
    tracking_mode: Mapped[str] = mapped_column(String, default="DISABLED")
    spam_quarantine_score: Mapped[int] = mapped_column(Integer, default=5)
    spam_reject_score: Mapped[int] = mapped_column(Integer, default=15)


class ProviderMessage(Base):
    __tablename__ = "provider_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_provider_message_tenant_idempotency"),
        UniqueConstraint("tenant_id", "correlation_id", name="uq_provider_message_tenant_correlation"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    correlation_id: Mapped[str] = mapped_column(String, index=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    sender: Mapped[str] = mapped_column(String)
    recipient: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    stream: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="CREATED", index=True)
    sandbox: Mapped[bool] = mapped_column(Boolean, default=True)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProviderUsageEvent(Base):
    __tablename__ = "provider_usage_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("provider_messages.id"), unique=True, index=True)
    stream: Mapped[str] = mapped_column(String)
    billable_units: Mapped[int] = mapped_column(Integer, default=1)
    result_category: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProviderEvent(Base):
    __tablename__ = "provider_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    message_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SandboxCapture(Base):
    __tablename__ = "sandbox_captures"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("provider_messages.id"), unique=True, index=True)
    envelope_from: Mapped[str] = mapped_column(String)
    envelope_to: Mapped[str] = mapped_column(String)
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProviderInbound(Base):
    __tablename__ = "provider_inbound"
    __table_args__ = (UniqueConstraint("tenant_id", "provider_event_id", name="uq_inbound_tenant_provider_event"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    provider_event_id: Mapped[str] = mapped_column(String, index=True)
    route_id: Mapped[str] = mapped_column(String, index=True)
    message_id_header: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    sender: Mapped[str] = mapped_column(String)
    recipient: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    text_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    html_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachments_json: Mapped[str] = mapped_column(Text, default="[]")
    auth_verdict: Mapped[str] = mapped_column(String, index=True)
    spf_result: Mapped[str] = mapped_column(String)
    dkim_result: Mapped[str] = mapped_column(String)
    dmarc_result: Mapped[str] = mapped_column(String)
    arc_result: Mapped[str] = mapped_column(String)
    dmarc_fail_action: Mapped[str] = mapped_column(String)
    disposition: Mapped[str] = mapped_column(String, default="ACCEPT", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TrackingToken(Base):
    __tablename__ = "tracking_tokens"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("provider_messages.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SmtpCredential(Base):
    __tablename__ = "smtp_credentials"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String)
    allowed_senders_json: Mapped[str] = mapped_column(Text)
    allowed_streams_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DkimKey(Base):
    __tablename__ = "dkim_keys"
    __table_args__ = (UniqueConstraint("domain_id", "selector", name="uq_dkim_domain_selector"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    domain_id: Mapped[str] = mapped_column(ForeignKey("provider_domains.id"), index=True)
    selector: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer)
    public_value: Mapped[str] = mapped_column(Text)
    private_secret_ref: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="PENDING_DNS", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderAudit(Base):
    __tablename__ = "provider_audit"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String)
    correlation_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)


class ProviderMailIn(BaseModel):
    sender: EmailStr
    recipient: str = Field(min_length=3, max_length=320)
    subject: str = Field(min_length=1, max_length=998)
    html: Optional[str] = Field(default=None, max_length=1_000_000)
    text: Optional[str] = Field(default=None, max_length=1_000_000)
    reply_to: Optional[EmailStr] = None
    stream: str = "TRANSACTIONAL"
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=20)
    sandbox: bool = True

    @field_validator("recipient")
    @classmethod
    def validate_recipient(cls, value: str) -> str:
        candidate = value.strip().lower()
        sink_domain = os.getenv("KLYROW_SANDBOX_DOMAIN", "klyrow-sink.test").lower()
        if candidate.endswith("@" + sink_domain) and candidate.count("@") == 1:
            return candidate
        try:
            return validate_email(candidate, check_deliverability=False).normalized
        except EmailNotValidError as exc:
            raise ValueError("invalid_recipient_address") from exc

    @model_validator(mode="after")
    def validate_content(self):
        self.stream = self.stream.upper()
        if self.stream not in STREAMS:
            raise ValueError("invalid_message_stream")
        if not self.html and not self.text:
            raise ValueError("message_content_required")
        return self


class CanonicalContent(BaseModel):
    subject: Optional[str] = Field(default=None, max_length=998)
    text: Optional[str] = Field(default=None, max_length=1_000_000)
    html: Optional[str] = Field(default=None, max_length=1_000_000)
    templateId: Optional[str] = None
    templateVersion: Optional[int] = Field(default=None, ge=1)
    variables: dict = Field(default_factory=dict)


class CanonicalEmailIn(BaseModel):
    channel: str = "email"
    sender: Optional[EmailStr] = None
    from_address: Optional[EmailStr] = Field(default=None, alias="from")
    to: list[str] = Field(min_length=1, max_length=1000)
    senderIdentityId: Optional[str] = None
    domainId: Optional[str] = None
    content: CanonicalContent
    scheduledAt: Optional[datetime] = None
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_step3_email(self):
        if self.channel != "email":
            raise ValueError("only channel=email is supported by this provider")
        if len(self.to) != 1:
            raise ValueError("Klyrow Step 3 accepts one recipient per provider request")
        self.to = [recipient.strip().lower() for recipient in self.to]
        if not self.content.text and not self.content.html:
            raise ValueError("Klyrow Step 3 requires rendered text or html content")
        return self


def provider_to_canonical_status(status: str) -> str:
    value = status.upper()
    if value in {"CREATED", "QUEUED"}:
        return "queued"
    if value in {"PROCESSING", "SUBMITTED", "SENT", "DEFERRED"}:
        return "dispatched" if value != "DEFERRED" else "indeterminate"
    if value == "DELIVERED":
        return "delivered"
    if value == "SUPPRESSED":
        return "suppressed"
    if value in {"BOUNCED_SOFT", "BOUNCED_HARD", "COMPLAINED", "FAILED", "DEAD_LETTER"}:
        return "failed"
    return "indeterminate"


def canonical_message_response(message: ProviderMessage) -> dict:
    return {
        "messageId": message.id,
        "tenantId": message.tenant_id,
        "channel": "email",
        "direction": "outbound",
        "status": provider_to_canonical_status(message.status),
        "correlationId": message.correlation_id,
        "idempotencyKey": message.idempotency_key,
        "operationId": None,
        "provider": "klyrow",
        "providerReference": message.provider_message_id,
        "failureCode": message.last_error,
        "failureMessage": message.last_error,
        "createdAt": message.created_at.isoformat(),
        "acceptedAt": message.created_at.isoformat(),
        "dispatchedAt": message.updated_at.isoformat() if message.status in {"PROCESSING", "SUBMITTED", "SENT", "DELIVERED"} else None,
        "completedAt": message.updated_at.isoformat() if provider_to_canonical_status(message.status) in {"delivered", "failed", "suppressed"} else None,
        "updatedAt": message.updated_at.isoformat(),
        "metadata": {"stream": message.stream, "sandbox": message.sandbox},
    }


class DomainRegisterIn(BaseModel):
    domain: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class SenderIn(BaseModel):
    domain_id: str
    email: EmailStr
    display_name: Optional[str] = Field(default=None, max_length=200)
    reply_to: Optional[EmailStr] = None
    stream: str = "TRANSACTIONAL"


class PolicyIn(BaseModel):
    sending_disabled: bool = True
    sandbox_mode: bool = True
    daily_limit: int = Field(default=1000, ge=0, le=10_000_000)
    hourly_limit: int = Field(default=100, ge=0, le=1_000_000)
    max_message_bytes: int = Field(default=10_000_000, ge=1024, le=50_000_000)
    max_attachment_bytes: int = Field(default=5_000_000, ge=0, le=25_000_000)
    allowed_test_recipients: list[EmailStr] = Field(default_factory=list, max_length=20)
    reputation_state: str = "GOOD"
    warmup_daily_limit: int = Field(default=100, ge=0, le=10_000_000)
    warmup_hourly_limit: int = Field(default=20, ge=0, le=1_000_000)
    warmup_growth_percent: int = Field(default=20, ge=0, le=100)
    ip_pool: str = "SHARED"
    tracking_mode: str = "DISABLED"
    spam_quarantine_score: int = Field(default=5, ge=0, le=1000)
    spam_reject_score: int = Field(default=15, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_spam_thresholds(self):
        if self.spam_reject_score <= self.spam_quarantine_score:
            raise ValueError("spam_reject_score_must_exceed_quarantine_score")
        return self


class InboundFixtureIn(BaseModel):
    provider_event_id: str = Field(min_length=8, max_length=200)
    envelope_to: EmailStr
    raw_message_b64: str = Field(min_length=4, max_length=35_000_000)
    destination_override: Optional[str] = None
    provider_spam_score: int = Field(default=0, ge=0, le=1000)
    spf_result: str = Field(pattern="^(PASS|FAIL|SOFTFAIL|NEUTRAL|NONE|TEMPERROR|PERMERROR)$")
    dkim_result: str = Field(pattern="^(PASS|FAIL|NONE|TEMPERROR|PERMERROR)$")
    dmarc_result: str = Field(pattern="^(PASS|FAIL|NONE|TEMPERROR|PERMERROR)$")
    arc_result: str = Field(pattern="^(PASS|FAIL|NONE)$")
    dmarc_fail_action: str = Field(pattern="^(REJECT|QUARANTINE|ACCEPT)$")


class PostalInboundAttachment(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=25_000_000)
    data: str = Field(max_length=35_000_000)


class PostalInboundHash(BaseModel):
    """Postal 3.3 Hash/BodyAsJSON payload signed over its exact body."""

    id: int = Field(ge=1)
    rcpt_to: EmailStr
    mail_from: str = Field(default="", max_length=320)
    subject: str = Field(default="", max_length=998)
    message_id: Optional[str] = Field(default=None, max_length=998)
    timestamp: float
    size: int = Field(ge=0, le=50_000_000)
    spam_status: str = Field(pattern="^(NotChecked|NotSpam|Spam)$")
    bounce: bool = False
    received_with_ssl: bool = False
    to: Optional[str] = Field(default=None, max_length=2000)
    cc: Optional[str] = Field(default=None, max_length=2000)
    from_address: Optional[str] = Field(default=None, alias="from", max_length=2000)
    date: Optional[str] = Field(default=None, max_length=200)
    in_reply_to: Optional[str] = Field(default=None, max_length=998)
    references: Optional[str] = Field(default=None, max_length=4000)
    html_body: Optional[str] = Field(default=None, max_length=1_000_000)
    plain_body: Optional[str] = Field(default=None, max_length=1_000_000)
    attachment_quantity: int = Field(default=0, ge=0, le=20)
    attachments: list[PostalInboundAttachment] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_attachments(self):
        if self.attachment_quantity != len(self.attachments):
            raise ValueError("postal_attachment_count_mismatch")
        return self


class SmtpCredentialIn(BaseModel):
    allowed_sender_ids: list[str] = Field(min_length=1, max_length=100)
    allowed_streams: list[str] = Field(default_factory=lambda: ["TRANSACTIONAL"], min_length=1, max_length=5)
    expires_in_days: int = Field(default=90, ge=1, le=365)


class SmtpPreflightIn(BaseModel):
    username: str = Field(min_length=8, max_length=200)
    password: str = Field(min_length=24, max_length=300)
    sender: EmailStr
    recipient: str = Field(min_length=3, max_length=320)
    stream: str = "TRANSACTIONAL"


class ProviderOutboxReconcileIn(BaseModel):
    reason: str = Field(min_length=8, max_length=500)
    limit: int = Field(default=50, ge=1, le=50)
    apply: bool = False
    confirmation: Optional[str] = None

    @model_validator(mode="after")
    def require_fixed_apply_confirmation(self):
        if self.apply and self.confirmation != "RECONCILE_PROVIDER_OUTBOX":
            raise ValueError("provider_outbox_reconcile_confirmation_required")
        return self


EXECUTABLE_SUFFIXES = {".exe", ".com", ".bat", ".cmd", ".ps1", ".js", ".jar", ".scr", ".msi", ".dll"}


def parse_inbound(raw: bytes, max_message_bytes: int, max_attachment_bytes: int) -> dict:
    if len(raw) > max_message_bytes:
        raise HTTPException(413, "inbound_message_too_large")
    try:
        message = BytesParser(policy=mime_policy.default).parsebytes(raw)
    except Exception as exc:
        raise HTTPException(422, "invalid_mime_message") from exc
    text_body = None
    html_body = None
    attachments = []
    attachment_contents = []
    disposition = "ACCEPT"
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        content_type = part.get_content_type()
        if filename is not None:
            clean = PurePath(filename).name
            if clean != filename or filename in {".", ".."} or "\\" in filename:
                raise HTTPException(422, "unsafe_attachment_filename")
            if len(content) > max_attachment_bytes:
                raise HTTPException(413, "inbound_attachment_too_large")
            if PurePath(clean.lower()).suffix in EXECUTABLE_SUFFIXES:
                disposition = "QUARANTINE"
            attachments.append({"filename": clean, "content_type": content_type, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            attachment_contents.append(content)
        elif content_type == "text/plain" and text_body is None:
            text_body = content.decode(part.get_content_charset() or "utf-8", errors="replace")
        elif content_type == "text/html" and html_body is None:
            html_body = content.decode(part.get_content_charset() or "utf-8", errors="replace")
    return {"message_id": message.get("Message-ID"), "from": message.get("From", ""), "to": message.get("To", ""),
        "cc": message.get("Cc"), "date": message.get("Date"), "in_reply_to": message.get("In-Reply-To"),
        "references": message.get("References"), "subject": message.get("Subject", ""), "text": text_body,
        "html": html_body, "attachments": attachments, "attachment_contents": attachment_contents,
        "disposition": disposition}


MIDDLEWARE_INBOUND_DESTINATIONS = {"odoo_helpdesk", "odoo_accounting"}


def middleware_destination_kind(route) -> str:
    """Return the closed Server A destination name for either route model."""
    destination = str(route.destination_kind)
    if destination == "SUPPORT":
        return "odoo_helpdesk"
    if destination == "ODOO":
        return "odoo_accounting" if "account" in str(route.destination_ref or "").lower() else "odoo_helpdesk"
    return destination


def middleware_inbound_eligible(route, disposition: str) -> bool:
    """Only accepted Odoo-bound mail crosses the protected Server A edge."""
    return disposition == "ACCEPT" and middleware_destination_kind(route) in MIDDLEWARE_INBOUND_DESTINATIONS


def middleware_inbound_payload(
    *,
    event_id: str,
    item: ProviderInbound,
    route,
    parsed: dict,
    occurred_at: str,
    payload_hash: str,
) -> dict:
    """Build the durable, replayable Server A inbound-mail contract."""
    metadata = list(parsed.get("attachments") or [])
    contents = list(parsed.get("attachment_contents") or [])
    if len(metadata) != len(contents):
        raise ValueError("inbound_attachment_content_unavailable")
    attachments = []
    for details, content in zip(metadata, contents, strict=True):
        if not isinstance(content, bytes):
            raise ValueError("invalid_inbound_attachment_content")
        attachments.append({**details, "data_b64": base64.b64encode(content).decode("ascii")})
    return {
        "event_id": event_id,
        "schema_version": "1.0",
        "event": "inbound.received",
        "timestamp": occurred_at,
        "occurred_at": occurred_at,
        "payload_hash": payload_hash,
        "tenant_id": item.tenant_id,
        "inbound_id": item.id,
        "provider_event_id": item.provider_event_id,
        "route_id": item.route_id,
        "destination_kind": middleware_destination_kind(route),
        "destination_ref": route.destination_ref,
        "disposition": item.disposition,
        "recipient": item.recipient,
        "sender": item.sender,
        "subject": item.subject,
        "message_id": parsed.get("message_id"),
        "in_reply_to": parsed.get("in_reply_to"),
        "references": parsed.get("references"),
        "date": parsed.get("date"),
        "cc": parsed.get("cc"),
        "text": parsed.get("text"),
        "html": parsed.get("html"),
        "attachments": attachments,
        "auth_verdict": item.auth_verdict,
        "spf_result": item.spf_result,
        "dkim_result": item.dkim_result,
        "dmarc_result": item.dmarc_result,
        "arc_result": item.arc_result,
        "dmarc_fail_action": item.dmarc_fail_action,
    }


def smtp_authorize(s: Session, payload: SmtpPreflightIn, tenant_id: str) -> SmtpCredential:
    credential = s.scalar(select(SmtpCredential).where(SmtpCredential.username == payload.username))
    # Return one indistinguishable denial for missing, foreign, revoked, expired, or invalid credentials.
    if not credential or credential.tenant_id != tenant_id or credential.status != "ACTIVE":
        raise HTTPException(401, "smtp_authentication_failed")
    expiry = credential.expires_at
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry and expiry <= now():
        raise HTTPException(401, "smtp_authentication_failed")
    try:
        smtp_hasher.verify(credential.secret_hash, payload.password)
    except VerifyMismatchError as exc:
        raise HTTPException(401, "smtp_authentication_failed") from exc
    sender = str(payload.sender).lower()
    if sender not in set(json.loads(credential.allowed_senders_json)):
        raise HTTPException(403, "smtp_sender_not_allowed")
    if payload.stream.upper() not in set(json.loads(credential.allowed_streams_json)):
        raise HTTPException(403, "smtp_stream_not_allowed")
    identity = s.scalar(select(SenderIdentity).where(SenderIdentity.tenant_id == tenant_id,
        SenderIdentity.email == sender, SenderIdentity.status == "ACTIVE"))
    if not identity:
        raise HTTPException(403, "smtp_sender_not_allowed")
    domain = s.scalar(select(ProviderDomain).where(ProviderDomain.id == identity.domain_id,
        ProviderDomain.tenant_id == tenant_id))
    if not domain or domain.status in {"SUSPENDED", "REMOVED"}:
        raise HTTPException(403, "smtp_sender_not_allowed")
    return credential


def generate_dkim_material(domain: ProviderDomain, version: int) -> tuple[str, str, str]:
    key_root = os.getenv("KLYROW_DKIM_KEY_DIR", "").strip()
    if not key_root:
        raise HTTPException(503, "dkim_secret_store_unavailable")
    root = os.path.realpath(key_root)
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    selector = "kly" + now().strftime("%Y%m%d") + "v" + str(version)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_der = private_key.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    public_value = "v=DKIM1; k=rsa; p=" + base64.b64encode(public_der).decode()
    domain_dir = os.path.realpath(os.path.join(root, domain.id))
    if os.path.commonpath([root, domain_dir]) != root:
        raise HTTPException(500, "dkim_secret_path_invalid")
    os.makedirs(domain_dir, mode=0o700, exist_ok=True)
    os.chmod(domain_dir, 0o700)
    path = os.path.join(domain_dir, selector + ".pem")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, private_pem)
    finally:
        os.close(descriptor)
    return selector, public_value, "file:" + path


def dns_txt(name: str) -> list[str]:
    try:
        return [b"".join(answer.strings).decode() for answer in dns.resolver.resolve(name, "TXT")]
    except Exception:
        return []


def dns_values(name: str, record_type: str) -> list[str]:
    try:
        return [str(answer).rstrip(".") for answer in dns.resolver.resolve(name, record_type)]
    except Exception:
        return []


def domain_dns_evidence(domain: ProviderDomain, active_key: Optional[DkimKey]) -> dict:
    spf_records = [value for value in dns_txt(domain.domain) if value.lower().startswith("v=spf1")]
    dmarc_records = [value for value in dns_txt("_dmarc." + domain.domain) if value.lower().startswith("v=dmarc1")]
    spf_token = os.getenv("KLYROW_SPF_TOKEN", "include:spf.klyrow.com").lower()
    mx_values = dns_values(domain.domain, "MX")
    approved_mx = os.getenv("KLYROW_MAIL_HOST", "mail.klyrow.com").lower()
    dkim_values = dns_txt(active_key.selector + "._domainkey." + domain.domain) if active_key else []
    return_path = os.getenv("KLYROW_RETURN_PATH_PREFIX", "bounce") + "." + domain.domain
    tracking = os.getenv("KLYROW_TRACKING_PREFIX", "track") + "." + domain.domain
    return_path_values = dns_values(return_path, "A") + dns_values(return_path, "CNAME")
    tracking_values = dns_values(tracking, "A") + dns_values(tracking, "CNAME")
    evidence = {
        "spf": len(spf_records) == 1 and spf_token in spf_records[0].lower(),
        "spf_record_count": len(spf_records),
        "dkim": bool(active_key and dkim_values.count(active_key.public_value) == 1),
        "dmarc": len(dmarc_records) == 1,
        "dmarc_record_count": len(dmarc_records),
        "return_path": bool(return_path_values),
        "tracking": bool(tracking_values),
        "mx": (not domain.inbound_enabled) or any(approved_mx in value.lower() for value in mx_values),
        "mx_required": domain.inbound_enabled,
        "mail_host": approved_mx,
    }
    evidence["verified"] = all(evidence[key] for key in ("spf", "dkim", "dmarc", "return_path", "tracking", "mx"))
    return evidence


def canonical_hash(payload: ProviderMailIn) -> str:
    return hashlib.sha256(payload.model_dump_json(exclude_none=True).encode()).hexdigest()


def audit_provider(s: Session, ctx: dict, action: str, outcome: str, correlation_id: Optional[str] = None, resource_id: Optional[str] = None):
    s.add(ProviderAudit(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], actor=ctx["sub"], action=action, outcome=outcome, correlation_id=correlation_id, resource_id=resource_id))


def policy_for(s: Session, tenant_id: str) -> TenantMailPolicy:
    policy = s.get(TenantMailPolicy, tenant_id)
    if not policy:
        policy = TenantMailPolicy(tenant_id=tenant_id)
        s.add(policy)
        s.flush()
    return policy


def reconcile_legacy_registry(s: Session) -> dict:
    """Idempotently migrate already-authoritative local registrations into the provider registry."""
    domains_created = 0
    senders_created = 0
    for legacy in s.scalars(select(Domain).where(Domain.verified == True)).all():
        item = s.scalar(select(ProviderDomain).where(ProviderDomain.domain == legacy.domain.lower()))
        if not item:
            item = ProviderDomain(id=str(uuid.uuid4()), tenant_id=legacy.tenant_id, domain=legacy.domain.lower(),
                status="VERIFIED", ownership_token=secrets.token_urlsafe(24), verified_at=now(), sending_enabled=False)
            s.add(item)
            s.flush()
            domains_created += 1
        if item.tenant_id != legacy.tenant_id:
            raise RuntimeError("provider_domain_ownership_conflict")
        policy_for(s, legacy.tenant_id)
        for allowed in s.scalars(select(AllowedSender).where(AllowedSender.tenant_id == legacy.tenant_id,
            AllowedSender.enabled == True)).all():
            if allowed.address.lower().rsplit("@", 1)[-1] != item.domain:
                continue
            sender = s.scalar(select(SenderIdentity).where(SenderIdentity.tenant_id == legacy.tenant_id,
                SenderIdentity.email == allowed.address.lower()))
            if not sender:
                s.add(SenderIdentity(id=str(uuid.uuid4()), tenant_id=legacy.tenant_id, domain_id=item.id,
                    email=allowed.address.lower(), stream="TRANSACTIONAL", status="ACTIVE"))
                senders_created += 1
    s.commit()
    return {"domains_created": domains_created, "senders_created": senders_created}


def preflight(payload: ProviderMailIn, ctx: dict, s: Session) -> dict:
    authorization = authorize_send(
        s,
        tenant_id=ctx["tenant"],
        sender=str(payload.sender),
        recipient=str(payload.recipient),
        stream=payload.stream,
        sandbox=payload.sandbox,
    )
    tenant = authorization["tenant"]
    from .operations import enforce_tenant_send_gate
    enforce_tenant_send_gate(s, ctx["tenant"])
    policy = policy_for(s, ctx["tenant"])
    if policy.reputation_state not in REPUTATION_STATES:
        raise HTTPException(503, "invalid_reputation_policy")
    if policy.reputation_state == "SUSPENDED":
        raise HTTPException(403, "tenant_reputation_suspended")
    if policy.sandbox_mode and not payload.sandbox:
        raise HTTPException(403, "tenant_sandbox_mode_required")
    if policy.sending_disabled and not payload.sandbox:
        raise HTTPException(403, "tenant_sending_disabled")
    sender = str(payload.sender).lower()
    sender_identity = s.scalar(select(SenderIdentity).where(SenderIdentity.tenant_id == ctx["tenant"], SenderIdentity.email == sender))
    legacy_sender = s.scalar(select(AllowedSender).where(AllowedSender.tenant_id == ctx["tenant"], AllowedSender.address == sender, AllowedSender.enabled == True))
    if sender_identity and sender_identity.status != "ACTIVE":
        raise HTTPException(403, "sender_identity_suspended")
    if sender_identity and sender_identity.stream != payload.stream:
        raise HTTPException(403, "sender_stream_not_allowed")
    if sender_identity is None and not legacy_sender:
        raise HTTPException(403, "sender_address_not_allowed")
    domain_name = sender.rsplit("@", 1)[1]
    provider_domain = s.scalar(select(ProviderDomain).where(ProviderDomain.tenant_id == ctx["tenant"], ProviderDomain.domain == domain_name))
    legacy_domain = s.scalar(select(Domain).where(Domain.tenant_id == ctx["tenant"], Domain.domain == domain_name, Domain.verified == True))
    if provider_domain:
        if provider_domain.status not in {"VERIFIED", "SENDING_ENABLED"}:
            raise HTTPException(422, "sender_domain_not_verified")
        if not payload.sandbox and (provider_domain.status != "SENDING_ENABLED" or not provider_domain.sending_enabled):
            raise HTTPException(403, "sender_domain_not_enabled")
    elif not legacy_domain:
        raise HTTPException(422, "sender_domain_not_verified")
    recipient = authorization["recipient"]
    body_size = len((payload.html or "").encode()) + len((payload.text or "").encode())
    attachment_size = sum(item.size for item in payload.attachments)
    if body_size + attachment_size > policy.max_message_bytes:
        raise HTTPException(413, "message_too_large")
    if any(item.size > policy.max_attachment_bytes for item in payload.attachments):
        raise HTTPException(413, "attachment_too_large")
    if any("/" in item.filename or "\\" in item.filename or item.filename in {".", ".."} for item in payload.attachments):
        raise HTTPException(422, "unsafe_attachment_filename")
    since_hour = now() - timedelta(hours=1)
    since_day = now() - timedelta(days=1)
    hour_count = s.scalar(select(func.count(ProviderMessage.id)).where(ProviderMessage.tenant_id == ctx["tenant"], ProviderMessage.created_at >= since_hour)) or 0
    day_count = s.scalar(select(func.count(ProviderMessage.id)).where(ProviderMessage.tenant_id == ctx["tenant"], ProviderMessage.created_at >= since_day)) or 0
    hourly_limit = min(policy.hourly_limit, policy.warmup_hourly_limit) if policy.warmup_hourly_limit else policy.hourly_limit
    daily_limit = min(policy.daily_limit, policy.warmup_daily_limit) if policy.warmup_daily_limit else policy.daily_limit
    if policy.reputation_state == "LIMITED":
        hourly_limit = max(1, hourly_limit // 2)
        daily_limit = max(1, daily_limit // 2)
    if hour_count >= hourly_limit or day_count >= min(daily_limit, tenant.quota):
        raise HTTPException(429, "provider_quota_exceeded")
    return {"allowed": True, "dry_run": True, "tenant_id": ctx["tenant"], "sender": sender, "recipient": recipient, "stream": payload.stream, "sandbox": payload.sandbox, "postal_submitted": False}


@router.post("/preflight")
def email_preflight(payload: ProviderMailIn, ctx=Depends(auth), s: Session = Depends(db), x_correlation_id: str = Header(min_length=8, max_length=128)):
    result = preflight(payload, ctx, s)
    audit_provider(s, ctx, "email.preflight", "allowed", x_correlation_id)
    s.commit()
    return {**result, "correlation_id": x_correlation_id}


@router.post("/send", status_code=202)
def email_send(payload: ProviderMailIn, ctx=Depends(auth), s: Session = Depends(db), idempotency_key: str = Header(min_length=8, max_length=200), x_correlation_id: str = Header(min_length=8, max_length=128)):
    authorize_send(s, tenant_id=ctx["tenant"], sender=str(payload.sender), recipient=str(payload.recipient), stream=payload.stream, sandbox=payload.sandbox)
    enforce_consent(s, ctx["tenant"], str(payload.recipient), payload.stream)
    result = preflight(payload, ctx, s)
    request_hash = canonical_hash(payload)
    existing = s.scalar(select(ProviderMessage).where(ProviderMessage.tenant_id == ctx["tenant"], ProviderMessage.idempotency_key == idempotency_key))
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        return {"message_id": existing.id, "status": existing.status, "request_id": existing.correlation_id, "sandbox": existing.sandbox}
    if s.scalar(select(ProviderMessage).where(ProviderMessage.tenant_id == ctx["tenant"], ProviderMessage.correlation_id == x_correlation_id)):
        raise HTTPException(409, "correlation_id_already_used")
    from .billing import UsageEvent
    from .guards import billing_identity
    subscription_id, price_id = billing_identity(s, ctx["tenant"], sandbox=payload.sandbox)
    message_payload=payload.model_dump(mode="json",exclude_none=True)
    if payload.stream=="MARKETING":
        from .preferences import one_click_unsubscribe_headers
        message_payload["headers"]=one_click_unsubscribe_headers(ctx["tenant"],result["recipient"])
    message = ProviderMessage(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], correlation_id=x_correlation_id, idempotency_key=idempotency_key, request_hash=request_hash, sender=result["sender"], recipient=result["recipient"], subject=payload.subject, payload_json=json.dumps(message_payload,separators=(",",":"),sort_keys=True), stream=payload.stream, status="QUEUED", sandbox=payload.sandbox)
    s.add(message)
    s.add(UsageEvent(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], subscription_id=subscription_id, message_id=message.id, event_key="accepted:provider:"+idempotency_key, unit="accepted_message", quantity=1, price_id=price_id))
    audit_provider(s, ctx, "email.queued", "accepted", x_correlation_id, message.id)
    s.commit()
    return {"message_id": message.id, "status": message.status, "request_id": x_correlation_id, "sandbox": message.sandbox}


@router.post("/communications/messages", status_code=202)
def canonical_email_send(payload: CanonicalEmailIn, ctx=Depends(auth), s: Session = Depends(db), idempotency_key: str = Header(min_length=8, max_length=200), x_correlation_id: str = Header(min_length=8, max_length=128)):
    if not ctx.get("service"):
        raise HTTPException(403, "middleware_service_identity_required")
    sender = payload.sender or payload.from_address
    if sender is None:
        raise HTTPException(422, "sender_required")
    stream = str(payload.metadata.get("stream") or "TRANSACTIONAL").upper()
    provider_payload = ProviderMailIn(
        sender=sender,
        recipient=str(payload.to[0]),
        subject=payload.content.subject or "",
        html=payload.content.html,
        text=payload.content.text,
        stream=stream,
        sandbox=True,
    )
    result = email_send(provider_payload, ctx, s, idempotency_key, x_correlation_id)
    message = s.get(ProviderMessage, result["message_id"])
    if not message:
        raise HTTPException(500, "provider_message_missing_after_acceptance")
    return canonical_message_response(message)


def process_one_sandbox(s: Session) -> Optional[str]:
    """Atomically claim one eligible sandbox message and persist its result."""
    message = s.scalar(select(ProviderMessage).where(
        ProviderMessage.status.in_(["QUEUED", "DEFERRED"]),
        ProviderMessage.sandbox == True,
        ProviderMessage.available_at <= now(),
    ).order_by(case({"SECURITY": 0, "SYSTEM": 10, "TRANSACTIONAL": 20, "MARKETING": 80, "BULK": 100}, value=ProviderMessage.stream, else_=1000), ProviderMessage.created_at).with_for_update(skip_locked=True).limit(1))
    if not message:
        return None
    message.status = "PROCESSING"
    message.attempts += 1
    message.lease_expires_at = now() + timedelta(minutes=2)
    s.flush()
    capture = s.scalar(select(SandboxCapture).where(SandboxCapture.message_id == message.id))
    if not capture:
        s.add(SandboxCapture(id=str(uuid.uuid4()), tenant_id=message.tenant_id, message_id=message.id,
            envelope_from=message.sender, envelope_to=message.recipient, content_json=message.payload_json))
    message.status = "DELIVERED"
    message.provider_message_id = "sink:" + message.id
    message.lease_expires_at = None
    message.updated_at = now()
    event_id = str(uuid.uuid4())
    event_payload = json.dumps({"event_id": event_id, "message_id": message.id,
        "tenant_id": message.tenant_id, "correlation_id": message.correlation_id,
        "event": "message.delivered", "provider": "internal-smtp-sink"}, separators=(",", ":"), sort_keys=True)
    s.add(ProviderEvent(id=event_id, tenant_id=message.tenant_id, message_id=message.id,
        kind="message.delivered", payload_json=event_payload))
    if not s.scalar(select(ProviderUsageEvent).where(ProviderUsageEvent.message_id == message.id)):
        s.add(ProviderUsageEvent(id=str(uuid.uuid4()), tenant_id=message.tenant_id,
            message_id=message.id, stream=message.stream, result_category="DELIVERED"))
    s.commit()
    return message.id


def recover_expired_leases(s: Session, max_attempts: int = 5) -> int:
    expired = list(s.scalars(select(ProviderMessage).where(ProviderMessage.status == "PROCESSING",
        ProviderMessage.lease_expires_at < now()).with_for_update(skip_locked=True)).all())
    for message in expired:
        message.status = "DEAD_LETTER" if message.attempts >= max_attempts else "INDETERMINATE"
        message.available_at = now() + timedelta(seconds=min(300, 2 ** max(message.attempts, 1)))
        message.lease_expires_at = None
        message.last_error = "worker_lease_expired"
        message.updated_at = now()
    s.commit()
    return len(expired)


def _inbound_route_for_event(s: Session, item: ProviderInbound):
    route = s.get(InboundRouteConfig, item.route_id)
    if route is not None:
        return route
    from .messaging import InboundRoute
    return s.get(InboundRoute, item.route_id)


def _upgrade_legacy_inbound_event(s: Session, event: ProviderEvent) -> tuple[str, Optional[dict]]:
    """Classify and, when possible, upgrade one historical inbound event."""
    item = s.get(ProviderInbound, event.message_id)
    route = _inbound_route_for_event(s, item) if item else None
    if item is None or route is None:
        return "blocked", None
    if not middleware_inbound_eligible(route, item.disposition):
        return "skipped", None
    existing = json.loads(event.payload_json)
    if all(existing.get(field) is not None for field in ("recipient", "sender", "subject", "destination_kind")):
        existing_attachments = list(existing.get("attachments") or [])
        if any(not isinstance(attachment, dict) or not attachment.get("data_b64") for attachment in existing_attachments):
            return "blocked", None
        existing["destination_kind"] = middleware_destination_kind(route)
        return "requeue", existing
    attachments = json.loads(item.attachments_json or "[]")
    if attachments:
        # Historical rows intentionally retained only attachment hashes. Never
        # fabricate content or acknowledge a lossy replay.
        return "blocked", None
    parsed = {
        "message_id": item.message_id_header,
        "in_reply_to": existing.get("in_reply_to"),
        "references": existing.get("references"),
        "date": existing.get("date"),
        "cc": existing.get("cc"),
        "text": item.text_body,
        "html": item.html_body,
        "attachments": [],
        "attachment_contents": [],
    }
    upgraded = middleware_inbound_payload(
        event_id=event.id,
        item=item,
        route=route,
        parsed=parsed,
        occurred_at=str(existing.get("occurred_at") or event.created_at.isoformat()),
        payload_hash=str(existing.get("payload_hash") or hashlib.sha256(event.payload_json.encode()).hexdigest()),
    )
    return "requeue", upgraded


def reconcile_provider_outbox_dead_letters(
    s: Session, *, tenant_id: str, limit: int = 50, apply: bool = False
) -> dict[str, int | bool]:
    """Plan or apply one bounded, auditable provider-outbox recovery batch."""
    if not 1 <= limit <= 50:
        raise ValueError("provider_outbox_reconcile_limit_out_of_range")
    from .main import SMTP_EVENT_MAP

    result: dict[str, int | bool] = {
        "apply": apply,
        "examined": 0,
        "events_requeued": 0,
        "events_skipped": 0,
        "events_blocked": 0,
        "usage_requeued": 0,
    }
    events = list(s.scalars(
        select(ProviderEvent)
        .where(ProviderEvent.state == "DEAD_LETTER", ProviderEvent.tenant_id == tenant_id)
        .order_by(ProviderEvent.created_at, ProviderEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all())
    for event in events:
        result["examined"] = int(result["examined"]) + 1
        upgraded = None
        if event.kind == "inbound.received":
            decision, upgraded = _upgrade_legacy_inbound_event(s, event)
        elif event.kind in SMTP_EVENT_MAP:
            decision = "requeue"
        else:
            decision = "blocked"
        result_key = {"requeue": "events_requeued", "skipped": "events_skipped", "blocked": "events_blocked"}[decision]
        result[result_key] = int(result[result_key]) + 1
        if not apply:
            continue
        if decision == "skipped":
            event.state = "SKIPPED"
            event.last_error = "middleware_delivery_not_applicable"
        elif decision == "requeue":
            if upgraded is not None:
                event.payload_json = json.dumps(upgraded, separators=(",", ":"), sort_keys=True)
            event.state = "RETRY"
            event.attempts = 0
            event.available_at = now()
            event.last_error = None
        else:
            # Unknown or lossy events remain on the existing critical alert;
            # reconciliation must never make unresolved data disappear.
            event.state = "DEAD_LETTER"
            event.last_error = "operator_reconciliation_required"
        event.updated_at = now()

    remaining = limit - len(events)
    if remaining:
        usages = list(s.scalars(
            select(ProviderUsageEvent)
            .where(ProviderUsageEvent.state == "DEAD_LETTER", ProviderUsageEvent.tenant_id == tenant_id)
            .order_by(ProviderUsageEvent.created_at, ProviderUsageEvent.id)
            .limit(remaining)
            .with_for_update(skip_locked=True)
        ).all())
        result["examined"] = int(result["examined"]) + len(usages)
        result["usage_requeued"] = int(result["usage_requeued"]) + len(usages)
        if apply:
            for usage in usages:
                usage.state = "RETRY"
                usage.attempts = 0
                usage.available_at = now()
                usage.last_error = None
    if apply:
        s.flush()
    return result


async def dispatch_provider_outbox(limit: int = 50) -> dict:
    from .provider_outbox_delivery import dispatch
    return await dispatch(limit)


async def provider_worker_loop():
    while True:
        try:
            from .main import DB
            with DB() as s:
                recover_expired_leases(s)
                for _ in range(50):
                    if not process_one_sandbox(s):
                        break
            await dispatch_provider_outbox()
        except Exception as exc:
            print(json.dumps({"level": "error", "system": "klyrow-mail-worker", "event": "worker_tick_failed",
                "error": type(exc).__name__}))
        await asyncio.sleep(2)


@router.post("/operations/process-sandbox")
def process_sandbox(ctx=Depends(auth), s: Session = Depends(db)):
    if ctx.get("role") != "platform_admin" or not ctx.get("service"):
        raise HTTPException(403, "operator_authorization_required")
    processed = []
    while len(processed) < 100:
        message_id = process_one_sandbox(s)
        if not message_id:
            break
        processed.append(message_id)
    return {"processed": processed, "count": len(processed), "provider": "internal-smtp-sink"}


@router.get("/messages/{message_id}")
def message_get(message_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    message = s.scalar(select(ProviderMessage).where(ProviderMessage.id == message_id, ProviderMessage.tenant_id == ctx["tenant"]))
    if not message:
        raise HTTPException(404, "message_not_found")
    return {"message_id": message.id, "provider_message_id": message.provider_message_id, "correlation_id": message.correlation_id, "status": message.status, "stream": message.stream, "sandbox": message.sandbox, "attempts": message.attempts}


@router.get("/communications/messages/{messageId}")
def canonical_message_get(messageId: str, ctx=Depends(auth), s: Session = Depends(db)):
    if not ctx.get("service"):
        raise HTTPException(403, "middleware_service_identity_required")
    message = s.scalar(select(ProviderMessage).where(ProviderMessage.id == messageId, ProviderMessage.tenant_id == ctx["tenant"]))
    if not message:
        raise HTTPException(404, "message_not_found")
    return canonical_message_response(message)


@router.get("/communications/messages/{messageId}/events")
def canonical_message_events(messageId: str, ctx=Depends(auth), s: Session = Depends(db)):
    if not ctx.get("service"):
        raise HTTPException(403, "middleware_service_identity_required")
    message = s.scalar(select(ProviderMessage).where(ProviderMessage.id == messageId, ProviderMessage.tenant_id == ctx["tenant"]))
    if not message:
        raise HTTPException(404, "message_not_found")
    events = list(s.scalars(select(ProviderEvent).where(ProviderEvent.message_id == messageId, ProviderEvent.tenant_id == ctx["tenant"]).order_by(ProviderEvent.created_at)).all())
    return {"items": [{
        "eventId": event.id,
        "messageId": event.message_id,
        "type": "codestra.communications.message.event.v1",
        "status": provider_to_canonical_status(event.kind.rsplit(".", 1)[-1]),
        "occurredAt": event.created_at.isoformat(),
        "provider": "klyrow",
        "providerReference": message.provider_message_id,
        "metadata": json.loads(event.payload_json),
    } for event in events]}


@router.post("/suppressions/check")
def suppression_check(recipient: EmailStr, ctx=Depends(auth), s: Session = Depends(db)):
    item = suppression_record(s, ctx["tenant"], str(recipient))
    return {"recipient": str(recipient).lower(), "suppressed": bool(item), "reason": item.reason if item else None}


@router.get("/reputation")
def reputation_metrics(ctx=Depends(auth), s: Session = Depends(db)):
    counts = {status: s.scalar(select(func.count(ProviderMessage.id)).where(
        ProviderMessage.tenant_id == ctx["tenant"], ProviderMessage.status == status)) or 0
        for status in MESSAGE_STATES}
    total = sum(counts.values())
    delivered = counts["DELIVERED"]
    bounced = counts["BOUNCED_SOFT"] + counts["BOUNCED_HARD"]
    policy = policy_for(s, ctx["tenant"])
    return {"tenant_id": ctx["tenant"], "state": policy.reputation_state, "volume": total,
        "delivered": delivered, "bounced": bounced, "hard_bounces": counts["BOUNCED_HARD"],
        "complaints": counts["COMPLAINED"], "failures": counts["FAILED"],
        "delivery_rate": delivered / total if total else 0,
        "bounce_rate": bounced / total if total else 0,
        "hard_bounce_rate": counts["BOUNCED_HARD"] / total if total else 0,
        "complaint_rate": counts["COMPLAINED"] / total if total else 0,
        "failure_rate": counts["FAILED"] / total if total else 0}


@router.get("/communications/provider-health")
def canonical_provider_health(ctx=Depends(auth), s: Session = Depends(db)):
    if not ctx.get("service"):
        raise HTTPException(403, "middleware_service_identity_required")
    queued = s.scalar(select(func.count(ProviderMessage.id)).where(ProviderMessage.tenant_id == ctx["tenant"], ProviderMessage.status.in_(["QUEUED", "DEFERRED", "PROCESSING"]))) or 0
    dead = s.scalar(select(func.count(ProviderMessage.id)).where(ProviderMessage.tenant_id == ctx["tenant"], ProviderMessage.status == "DEAD_LETTER")) or 0
    status = "disabled" if os.getenv("KLYROW_SAFE_MODE", "true").lower() == "true" else ("degraded" if dead else "healthy")
    return {"status": status, "checkedAt": now().isoformat(), "providers": [{"provider": "klyrow", "channel": "email", "status": status, "reason": "safe_mode" if status == "disabled" else None, "queueDepth": queued}]}


@router.get("/communications/reputation")
def canonical_reputation(ctx=Depends(auth), s: Session = Depends(db)):
    if not ctx.get("service"):
        raise HTTPException(403, "middleware_service_identity_required")
    raw = reputation_metrics(ctx, s)
    state_map = {"GOOD": "good", "WATCH": "watch", "LIMITED": "limited", "SUSPENDED": "suspended"}
    domains = list(s.scalars(select(ProviderDomain).where(ProviderDomain.tenant_id == ctx["tenant"])).all())
    return {
        "status": state_map.get(raw["state"], "watch"),
        "checkedAt": now().isoformat(),
        "domains": [{"domain": item.domain, "status": state_map.get(raw["state"], "watch")} for item in domains],
        "providers": [{"provider": "klyrow", "channel": "email", "status": state_map.get(raw["state"], "watch"), "queueDepth": raw["volume"] - raw["delivered"]}],
        "metrics": raw,
    }


@router.post("/domains/register", status_code=201)
def domain_register(payload: DomainRegisterIn, ctx=Depends(auth), s: Session = Depends(db)):
    require_permission(ctx, "domain.manage")
    name = payload.domain.lower()
    existing = s.scalar(select(ProviderDomain).where(ProviderDomain.domain == name))
    if existing:
        if existing.tenant_id != ctx["tenant"]:
            raise HTTPException(409, "domain_owned_by_another_tenant")
        return {"id": existing.id, "domain": existing.domain, "status": existing.status, "verification_record": "_klyrow-verification." + existing.domain, "verification_value": "klyrow=" + existing.ownership_token}
    token = uuid.uuid4().hex
    item = ProviderDomain(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], domain=name, status="DNS_REQUIRED", ownership_token=token)
    s.add(item)
    audit_provider(s, ctx, "domain.registered", "accepted", resource_id=item.id)
    s.commit()
    return {"id": item.id, "domain": item.domain, "status": item.status, "verification_record": "_klyrow-verification." + item.domain, "verification_value": "klyrow=" + token}


@router.post("/domains/verify")
def domain_verify(domain_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    from .business_events import enqueue_named_event
    require_permission(ctx, "domain.manage")
    item = s.scalar(select(ProviderDomain).where(ProviderDomain.id == domain_id, ProviderDomain.tenant_id == ctx["tenant"]))
    if not item:
        raise HTTPException(404, "domain_not_found")
    legacy = s.scalar(select(Domain).where(Domain.tenant_id == ctx["tenant"], Domain.domain == item.domain, Domain.verified == True))
    if not legacy:
        item.status = "DNS_REQUIRED"
        enqueue_named_event(s, event_type="klyrow.domain.status", tenant_id=ctx["tenant"],
            aggregate_id=item.id, causation_id=domain_id,
            data={"domain_id":item.id,"domain":item.domain,"status":item.status,"verified_at":None})
        s.commit()
        return {"domain": item.domain, "status": item.status, "verified": False}
    item.status = "VERIFIED"
    item.verified_at = now()
    enqueue_named_event(s, event_type="klyrow.domain.status", tenant_id=ctx["tenant"],
        aggregate_id=item.id, causation_id=domain_id,
        data={"domain_id":item.id,"domain":item.domain,"status":item.status,"verified_at":item.verified_at})
    s.commit()
    return {"domain": item.domain, "status": item.status, "verified": True}


@router.post("/domains/{domain_id}/dns-check")
def domain_dns_check(domain_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    from .business_events import enqueue_named_event
    require_permission(ctx, "domain.manage")
    domain = s.scalar(select(ProviderDomain).where(ProviderDomain.id == domain_id,
        ProviderDomain.tenant_id == ctx["tenant"]))
    if not domain:
        raise HTTPException(404, "domain_not_found")
    active_key = s.scalar(select(DkimKey).where(DkimKey.domain_id == domain.id,
        DkimKey.status == "ACTIVE").order_by(DkimKey.version.desc()).limit(1))
    evidence = domain_dns_evidence(domain, active_key)
    if evidence["verified"] and domain.status not in {"SUSPENDED", "REMOVED"}:
        domain.status = "SENDING_ENABLED" if domain.sending_enabled else "VERIFIED"
    elif domain.status not in {"SUSPENDED", "REMOVED"}:
        domain.status = "DNS_REQUIRED"
        domain.sending_enabled = False
    audit_provider(s, ctx, "domain.dns_checked", "verified" if evidence["verified"] else "dns_required",
        resource_id=domain.id)
    enqueue_named_event(s, event_type="klyrow.domain.status", tenant_id=ctx["tenant"],
        aggregate_id=domain.id, causation_id=domain_id,
        data={"domain_id":domain.id,"domain":domain.domain,"status":domain.status,
              "verified_at":domain.verified_at})
    s.commit()
    return {"domain": domain.domain, "status": domain.status, **evidence}


@router.get("/communications/domains")
def canonical_domains(ctx=Depends(auth), s: Session = Depends(db)):
    if not ctx.get("service"):
        raise HTTPException(403, "middleware_service_identity_required")
    status_map = {
        "PENDING": "pending",
        "DNS_REQUIRED": "dns_required",
        "VERIFYING": "verifying",
        "VERIFIED": "verified",
        "SENDING_ENABLED": "sending_enabled",
        "SUSPENDED": "suspended",
        "REMOVED": "removed",
    }
    domains = list(s.scalars(select(ProviderDomain).where(ProviderDomain.tenant_id == ctx["tenant"])).all())
    items = []
    for domain in domains:
        active_key = s.scalar(select(DkimKey).where(DkimKey.domain_id == domain.id, DkimKey.status == "ACTIVE").order_by(DkimKey.version.desc()).limit(1))
        items.append({
            "domainId": domain.id,
            "domain": domain.domain,
            "status": status_map.get(domain.status, "pending"),
            "checks": {
                "spf": "unknown",
                "dkim": "valid" if active_key else "pending",
                "dmarc": "unknown",
                "reverseDns": "unknown",
                "tls": "unknown",
                "bimi": "not_configured",
            },
            "provider": "klyrow",
            "dkimSelectors": [active_key.selector] if active_key else [],
            "createdAt": domain.created_at.isoformat(),
            "updatedAt": (domain.verified_at or domain.created_at).isoformat(),
        })
    return {"items": items}


@router.post("/senders", status_code=201)
def sender_create(payload: SenderIn, ctx=Depends(auth), s: Session = Depends(db)):
    require_permission(ctx, "sender.manage")
    domain = s.scalar(select(ProviderDomain).where(ProviderDomain.id == payload.domain_id, ProviderDomain.tenant_id == ctx["tenant"]))
    if not domain or domain.status not in {"VERIFIED", "SENDING_ENABLED"}:
        raise HTTPException(422, "sender_domain_not_verified")
    email = str(payload.email).lower()
    if email.rsplit("@", 1)[1] != domain.domain:
        raise HTTPException(422, "sender_domain_mismatch")
    if payload.stream.upper() not in STREAMS:
        raise HTTPException(422, "invalid_message_stream")
    allowed = s.scalar(select(AllowedSender).where(AllowedSender.tenant_id == ctx["tenant"], AllowedSender.address == email, AllowedSender.enabled == True))
    if not allowed:
        raise HTTPException(403, "sender_address_not_allowed")
    item = s.scalar(select(SenderIdentity).where(SenderIdentity.tenant_id == ctx["tenant"], SenderIdentity.email == email))
    if not item:
        item = SenderIdentity(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], domain_id=domain.id, email=email, display_name=payload.display_name, reply_to=str(payload.reply_to).lower() if payload.reply_to else None, stream=payload.stream.upper(), status="ACTIVE")
        s.add(item)
    s.commit()
    return {"id": item.id, "email": item.email, "status": item.status, "stream": item.stream}


@router.put("/policy")
def policy_update(payload: PolicyIn, ctx=Depends(auth), s: Session = Depends(db)):
    if ctx.get("role") not in {"platform_admin", "tenant_admin"}:
        raise HTTPException(403, "insufficient_role")
    if payload.reputation_state not in REPUTATION_STATES:
        raise HTTPException(422, "invalid_reputation_state")
    if payload.ip_pool not in {"SHARED", "DEDICATED", "CUSTOM"}:
        raise HTTPException(422, "invalid_ip_pool")
    if payload.tracking_mode not in {"DISABLED", "OPEN", "CLICK", "OPEN_CLICK"}:
        raise HTTPException(422, "invalid_tracking_mode")
    item = policy_for(s, ctx["tenant"])
    item.sending_disabled = payload.sending_disabled
    item.sandbox_mode = payload.sandbox_mode
    item.daily_limit = payload.daily_limit
    item.hourly_limit = payload.hourly_limit
    item.max_message_bytes = payload.max_message_bytes
    item.max_attachment_bytes = payload.max_attachment_bytes
    item.allowed_test_recipients_json = json.dumps(sorted({str(v).lower() for v in payload.allowed_test_recipients}))
    item.reputation_state = payload.reputation_state
    item.warmup_daily_limit = payload.warmup_daily_limit
    item.warmup_hourly_limit = payload.warmup_hourly_limit
    item.warmup_growth_percent = payload.warmup_growth_percent
    item.ip_pool = payload.ip_pool
    item.tracking_mode = payload.tracking_mode
    item.spam_quarantine_score = payload.spam_quarantine_score
    item.spam_reject_score = payload.spam_reject_score
    audit_provider(s, ctx, "policy.updated", "accepted")
    s.commit()
    return {"sending_disabled": item.sending_disabled, "sandbox_mode": item.sandbox_mode,
        "reputation_state": item.reputation_state, "ip_pool": item.ip_pool,
        "tracking_mode": item.tracking_mode, "spam_quarantine_score": item.spam_quarantine_score,
        "spam_reject_score": item.spam_reject_score, "warmup_daily_limit": item.warmup_daily_limit,
        "warmup_hourly_limit": item.warmup_hourly_limit}


@router.post("/smtp/credentials", status_code=201)
def smtp_credential_create(payload: SmtpCredentialIn, ctx=Depends(auth), s: Session = Depends(db)):
    from .secret_responses import record_secret_response, response_metadata
    if ctx.get("role") not in {"platform_admin", "tenant_admin"}:
        raise HTTPException(403, "insufficient_role")
    senders = list(s.scalars(select(SenderIdentity).where(SenderIdentity.id.in_(payload.allowed_sender_ids),
        SenderIdentity.tenant_id == ctx["tenant"], SenderIdentity.status == "ACTIVE")).all())
    if len(senders) != len(set(payload.allowed_sender_ids)):
        raise HTTPException(422, "smtp_sender_identity_invalid")
    streams = {stream.upper() for stream in payload.allowed_streams}
    if not streams or not streams.issubset(STREAMS):
        raise HTTPException(422, "smtp_stream_invalid")
    secret = secrets.token_urlsafe(36)
    item = SmtpCredential(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], username="kly_" + secrets.token_urlsafe(18),
        secret_hash=smtp_hasher.hash(secret), allowed_senders_json=json.dumps(sorted({sender.email for sender in senders})),
        allowed_streams_json=json.dumps(sorted(streams)), expires_at=now() + timedelta(days=payload.expires_in_days))
    s.add(item)
    result = {"credential_id": item.id, "username": item.username, "password": secret,
        "secret_display": "ONCE", "expires_at": item.expires_at.isoformat()}
    secret_response = record_secret_response(s, tenant_id=ctx["tenant"], resource_type="SMTP_CREDENTIAL",
        resource_id=item.id, action="CREATE", payload=result, actor=ctx["sub"])
    audit_provider(s, ctx, "smtp_credential.created", "accepted", resource_id=item.id)
    s.commit()
    return {"credential_id": item.id, "username": item.username, "password": secret,
        "secret_display": "ONCE", "expires_at": item.expires_at, **response_metadata(secret_response)}


@router.post("/smtp/credentials/{credential_id}/rotate")
def smtp_credential_rotate(credential_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    from .secret_responses import record_secret_response, response_metadata
    require_permission(ctx, "credential.manage")
    item = s.scalar(select(SmtpCredential).where(SmtpCredential.id == credential_id,
        SmtpCredential.tenant_id == ctx["tenant"], SmtpCredential.status == "ACTIVE"))
    if not item:
        raise HTTPException(404, "smtp_credential_not_found")
    secret = secrets.token_urlsafe(36)
    item.secret_hash = smtp_hasher.hash(secret)
    item.rotated_at = now()
    result = {"credential_id": item.id, "username": item.username, "password": secret,
        "secret_display": "ONCE"}
    secret_response = record_secret_response(s, tenant_id=ctx["tenant"], resource_type="SMTP_CREDENTIAL",
        resource_id=item.id, action="ROTATE", payload=result, actor=ctx["sub"])
    audit_provider(s, ctx, "smtp_credential.rotated", "accepted", resource_id=item.id)
    s.commit()
    return {**result, **response_metadata(secret_response)}


@router.post("/smtp/credentials/{credential_id}/revoke", status_code=204)
def smtp_credential_revoke(credential_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    require_permission(ctx, "credential.manage")
    item = s.scalar(select(SmtpCredential).where(SmtpCredential.id == credential_id,
        SmtpCredential.tenant_id == ctx["tenant"]))
    if not item:
        raise HTTPException(404, "smtp_credential_not_found")
    item.status = "REVOKED"
    audit_provider(s, ctx, "smtp_credential.revoked", "accepted", resource_id=item.id)
    s.commit()


@router.post("/smtp/preflight")
def smtp_preflight(payload: SmtpPreflightIn, ctx=Depends(auth), s: Session = Depends(db)):
    credential = smtp_authorize(s, payload, ctx["tenant"])
    policy = policy_for(s, ctx["tenant"])
    recipient = payload.recipient.lower()
    sink_domain = os.getenv("KLYROW_SANDBOX_DOMAIN", "klyrow-sink.test")
    if policy.sandbox_mode and not recipient.endswith("@" + sink_domain):
        raise HTTPException(403, "sandbox_recipient_not_allowed")
    return {"authorized": True, "tenant_id": credential.tenant_id, "sender": str(payload.sender).lower(),
        "stream": payload.stream.upper(), "dry_run": True, "postal_submitted": False}


@router.post("/domains/{domain_id}/suspend")
def domain_suspend(domain_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    require_permission(ctx, "domain.manage")
    item = s.scalar(select(ProviderDomain).where(ProviderDomain.id == domain_id, ProviderDomain.tenant_id == ctx["tenant"]))
    if not item:
        raise HTTPException(404, "domain_not_found")
    item.status = "SUSPENDED"
    item.sending_enabled = False
    s.query(SenderIdentity).filter(SenderIdentity.tenant_id == ctx["tenant"], SenderIdentity.domain_id == item.id).update({SenderIdentity.status: "SUSPENDED"})
    audit_provider(s, ctx, "domain.suspended", "accepted", resource_id=item.id)
    s.commit()
    return {"id": item.id, "status": item.status}


@router.post("/domains/{domain_id}/dkim/rotate", status_code=201)
def dkim_rotate(domain_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    if ctx.get("role") not in {"platform_admin", "tenant_admin"}:
        raise HTTPException(403, "insufficient_role")
    domain = s.scalar(select(ProviderDomain).where(ProviderDomain.id == domain_id,
        ProviderDomain.tenant_id == ctx["tenant"]))
    if not domain or domain.status not in {"VERIFIED", "SENDING_ENABLED"}:
        raise HTTPException(422, "dkim_domain_not_verified")
    version = (s.scalar(select(func.max(DkimKey.version)).where(DkimKey.domain_id == domain.id)) or 0) + 1
    selector, public_value, secret_ref = generate_dkim_material(domain, version)
    item = DkimKey(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], domain_id=domain.id, selector=selector,
        version=version, public_value=public_value, private_secret_ref=secret_ref, status="PENDING_DNS")
    s.add(item)
    audit_provider(s, ctx, "dkim.rotation.created", "pending_dns", resource_id=item.id)
    s.commit()
    return {"dkim_key_id": item.id, "selector": selector, "status": item.status,
        "dns": {"type": "TXT", "name": selector + "._domainkey." + domain.domain,
            "value": public_value}, "private_key": "PROTECTED_SECRET_REFERENCE"}


@router.post("/domains/{domain_id}/dkim/{key_id}/verify")
def dkim_verify(domain_id: str, key_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    require_permission(ctx, "domain.manage")
    domain = s.scalar(select(ProviderDomain).where(ProviderDomain.id == domain_id,
        ProviderDomain.tenant_id == ctx["tenant"]))
    key = s.scalar(select(DkimKey).where(DkimKey.id == key_id, DkimKey.domain_id == domain_id,
        DkimKey.tenant_id == ctx["tenant"]))
    if not domain or not key:
        raise HTTPException(404, "dkim_key_not_found")
    try:
        values = [b"".join(answer.strings).decode() for answer in dns.resolver.resolve(
            key.selector + "._domainkey." + domain.domain, "TXT")]
    except Exception:
        return {"verified": False, "status": key.status, "reason": "DNS_REQUIRED"}
    if values.count(key.public_value) != 1:
        return {"verified": False, "status": key.status, "reason": "DKIM_VALUE_MISMATCH"}
    prior = list(s.scalars(select(DkimKey).where(DkimKey.domain_id == domain.id,
        DkimKey.status == "ACTIVE", DkimKey.id != key.id)).all())
    for old in prior:
        old.status = "RETIRED"
        old.retired_at = now()
    key.status = "ACTIVE"
    key.activated_at = now()
    domain.dkim_selector = key.selector
    domain.dkim_key_version = key.version
    audit_provider(s, ctx, "dkim.rotation.activated", "verified", resource_id=key.id)
    s.commit()
    return {"verified": True, "status": key.status, "selector": key.selector,
        "retired_prior_keys": len(prior)}


@router.post("/senders/{sender_id}/suspend")
def sender_suspend(sender_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    require_permission(ctx, "sender.manage")
    item = s.scalar(select(SenderIdentity).where(SenderIdentity.id == sender_id, SenderIdentity.tenant_id == ctx["tenant"]))
    if not item:
        raise HTTPException(404, "sender_not_found")
    item.status = "SUSPENDED"
    audit_provider(s, ctx, "sender.suspended", "accepted", resource_id=item.id)
    s.commit()
    return {"id": item.id, "status": item.status}


@router.post("/webhooks/test")
def webhook_test(ctx=Depends(auth), x_correlation_id: str = Header(min_length=8, max_length=128)):
    return {"accepted": True, "dry_run": True, "event": "provider.webhook.test", "correlation_id": x_correlation_id, "delivered": False}


@router.post("/inbound/receive", status_code=202)
def inbound_receive(payload: InboundFixtureIn, ctx=Depends(auth), s: Session = Depends(db)):
    if payload.destination_override is not None:
        raise HTTPException(403, "client_destination_override_denied")
    recipient = str(payload.envelope_to).lower()
    route = s.scalar(select(InboundRouteConfig).where(
        InboundRouteConfig.tenant_id == ctx["tenant"],
        InboundRouteConfig.address == recipient,
        InboundRouteConfig.verified == True,
        InboundRouteConfig.enabled == True,
    ))
    if not route:
        raise HTTPException(404, "inbound_recipient_not_configured")
    existing = s.scalar(select(ProviderInbound).where(
        ProviderInbound.tenant_id == ctx["tenant"],
        ProviderInbound.provider_event_id == payload.provider_event_id,
    ))
    if existing:
        return {"inbound_id": existing.id, "duplicate": True, "disposition": existing.disposition}
    try:
        raw = base64.b64decode(payload.raw_message_b64, validate=True)
    except ValueError as exc:
        raise HTTPException(422, "invalid_mime_encoding") from exc
    tenant_policy = policy_for(s, ctx["tenant"])
    parsed = parse_inbound(raw, tenant_policy.max_message_bytes, tenant_policy.max_attachment_bytes)
    auth_verdict = "PASS" if payload.dmarc_result == "PASS" else "FAIL"
    if auth_verdict == "FAIL" and payload.dmarc_fail_action == "REJECT":
        parsed["disposition"] = "REJECT"
    elif auth_verdict == "FAIL" and payload.dmarc_fail_action == "QUARANTINE":
        parsed["disposition"] = "QUARANTINE"
    if payload.provider_spam_score >= tenant_policy.spam_reject_score:
        parsed["disposition"] = "REJECT"
    elif payload.provider_spam_score >= tenant_policy.spam_quarantine_score:
        parsed["disposition"] = "QUARANTINE"
    duplicate_message = None
    if parsed["message_id"]:
        duplicate_message = s.scalar(select(ProviderInbound).where(
            ProviderInbound.tenant_id == ctx["tenant"], ProviderInbound.route_id == route.id,
            ProviderInbound.message_id_header == parsed["message_id"],
        ))
    if duplicate_message:
        return {"inbound_id": duplicate_message.id, "duplicate": True, "disposition": duplicate_message.disposition}
    item = ProviderInbound(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], provider_event_id=payload.provider_event_id,
        route_id=route.id, message_id_header=parsed["message_id"], sender=parsed["from"], recipient=recipient,
        subject=parsed["subject"], text_body=parsed["text"], html_body=parsed["html"],
        attachments_json=json.dumps(parsed["attachments"], separators=(",", ":"), sort_keys=True),
        auth_verdict=auth_verdict, spf_result=payload.spf_result, dkim_result=payload.dkim_result,
        dmarc_result=payload.dmarc_result, arc_result=payload.arc_result,
        dmarc_fail_action=payload.dmarc_fail_action,
        disposition=parsed["disposition"])
    s.add(item)
    if middleware_inbound_eligible(route, item.disposition):
        event_id = str(uuid.uuid4())
        event_payload = middleware_inbound_payload(
            event_id=event_id,
            item=item,
            route=route,
            parsed=parsed,
            occurred_at=now().isoformat(),
            payload_hash=hashlib.sha256(raw).hexdigest(),
        )
        s.add(ProviderEvent(
            id=event_id,
            tenant_id=ctx["tenant"],
            message_id=item.id,
            kind="inbound.received",
            payload_json=json.dumps(event_payload, separators=(",", ":"), sort_keys=True),
        ))
    # The provider pipeline remains authoritative for SPF/DKIM/DMARC and spam
    # disposition. Webmail receives only the already-attributed, accepted or
    # quarantined tenant record; rejected messages are ignored by the helper.
    from .webmail import capture_provider_inbound

    capture_provider_inbound(s, route, item, parsed)
    audit_provider(s, ctx, "inbound.received", item.disposition, resource_id=item.id)
    s.commit()
    return {"inbound_id": item.id, "duplicate": False, "disposition": item.disposition,
        "route_id": route.id, "attachments": parsed["attachments"]}


@status_router.post("/v1/webhooks/postal-inbound", status_code=202)
async def postal_inbound(
    request: Request,
    x_postal_signature_256: str = Header(default=""),
    s: Session = Depends(db),
):
    """Persist one timestamp-bounded Postal-signed message before projection."""
    body = await request.body()
    if len(body) > 70_000_000:
        raise HTTPException(413, "postal_payload_too_large")
    verify_postal_signature(body, x_postal_signature_256)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "postal_json_required")
    try:
        payload = PostalInboundHash.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(422, "invalid_postal_inbound_payload") from exc
    current_time = time.time()
    if payload.timestamp > current_time + 300 or payload.timestamp < current_time - 86_400:
        raise HTTPException(401, "expired_postal_event")

    recipient = str(payload.rcpt_to).lower()
    configured_routes = list(s.scalars(select(InboundRouteConfig).where(
        InboundRouteConfig.address == recipient,
        InboundRouteConfig.verified == True,
        InboundRouteConfig.enabled == True,
    )).all())
    from .messaging import DomainClaim, InboundRoute

    api_routes = list(s.scalars(select(InboundRoute).join(
        DomainClaim, DomainClaim.id == InboundRoute.domain_claim_id,
    ).where(
        InboundRoute.recipient == recipient,
        InboundRoute.enabled == True,
        InboundRoute.wildcard == False,
        DomainClaim.state.in_({"VERIFIED", "SENDING_ENABLED"}),
    )).all())
    routes = configured_routes + api_routes
    if len(routes) != 1:
        raise HTTPException(404, "inbound_recipient_not_configured")
    route = routes[0]
    tenant = s.scalar(select(Tenant).where(Tenant.id == route.tenant_id, Tenant.enabled == True))
    if not tenant:
        raise HTTPException(403, "tenant_suspended")

    provider_event_id = f"postal:{route.id}:{payload.id}"
    existing = s.scalar(select(ProviderInbound).where(
        ProviderInbound.tenant_id == route.tenant_id,
        ProviderInbound.provider_event_id == provider_event_id,
    ))
    if existing:
        return {"accepted": True, "inbound_id": existing.id, "duplicate": True,
            "disposition": existing.disposition}

    tenant_policy = policy_for(s, route.tenant_id)
    route_message_limit = getattr(route, "max_bytes", tenant_policy.max_message_bytes)
    if payload.size > min(tenant_policy.max_message_bytes, route_message_limit):
        raise HTTPException(413, "inbound_message_too_large")
    attachment_metadata = []
    attachment_contents = []
    for attachment in payload.attachments:
        filename = PurePath(attachment.filename).name
        if filename != attachment.filename or filename in {".", ".."} or "\\" in attachment.filename:
            raise HTTPException(422, "unsafe_attachment_filename")
        try:
            compact = b"".join(attachment.data.encode("ascii").split())
            content = base64.b64decode(compact, validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise HTTPException(422, "invalid_attachment_encoding") from exc
        if len(content) != attachment.size:
            raise HTTPException(422, "postal_attachment_size_mismatch")
        if len(content) > tenant_policy.max_attachment_bytes:
            raise HTTPException(413, "inbound_attachment_too_large")
        attachment_metadata.append({
            "filename": filename,
            "content_type": attachment.content_type,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
        attachment_contents.append(content)

    parsed = {
        "message_id": payload.message_id,
        "from": payload.from_address or payload.mail_from,
        "to": payload.to or recipient,
        "cc": payload.cc,
        "date": payload.date,
        "in_reply_to": payload.in_reply_to,
        "references": payload.references,
        "subject": payload.subject,
        "text": payload.plain_body,
        "html": payload.html_body,
        "attachments": attachment_metadata,
        "attachment_contents": attachment_contents,
        # Postal authenticates the transport, but Hash does not carry trusted
        # SPF/DKIM/DMARC results. Keep all such mail quarantined fail closed.
        "disposition": "QUARANTINE",
    }
    if parsed["message_id"]:
        duplicate = s.scalar(select(ProviderInbound).where(
            ProviderInbound.tenant_id == route.tenant_id,
            ProviderInbound.route_id == route.id,
            ProviderInbound.message_id_header == parsed["message_id"],
        ))
        if duplicate:
            return {"accepted": True, "inbound_id": duplicate.id, "duplicate": True,
                "disposition": duplicate.disposition}

    item = ProviderInbound(
        id=str(uuid.uuid4()), tenant_id=route.tenant_id,
        provider_event_id=provider_event_id, route_id=route.id,
        message_id_header=parsed["message_id"], sender=parsed["from"], recipient=recipient,
        subject=parsed["subject"], text_body=parsed["text"], html_body=parsed["html"],
        attachments_json=json.dumps(attachment_metadata, separators=(",", ":"), sort_keys=True),
        auth_verdict="LEGACY_UNVERIFIED", spf_result="NONE", dkim_result="NONE",
        dmarc_result="NONE", arc_result="NONE", dmarc_fail_action="QUARANTINE",
        disposition="QUARANTINE",
    )
    s.add(item)
    if middleware_inbound_eligible(route, item.disposition):
        event_id = str(uuid.uuid4())
        normalized = middleware_inbound_payload(
            event_id=event_id,
            item=item,
            route=route,
            parsed=parsed,
            occurred_at=datetime.fromtimestamp(payload.timestamp, tz=timezone.utc).isoformat(),
            payload_hash=hashlib.sha256(body).hexdigest(),
        )
        s.add(ProviderEvent(
            id=event_id,
            tenant_id=route.tenant_id,
            message_id=item.id,
            kind="inbound.received",
            payload_json=json.dumps(normalized, separators=(",", ":"), sort_keys=True),
        ))
    if isinstance(route, InboundRouteConfig):
        from .webmail import capture_provider_inbound
        capture_provider_inbound(s, route, item, parsed)
    s.add(ProviderAudit(
        id=str(uuid.uuid4()), tenant_id=route.tenant_id, actor="provider:postal",
        action="inbound.received", outcome=item.disposition, resource_id=item.id,
    ))
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        winner = s.scalar(select(ProviderInbound).where(
            ProviderInbound.tenant_id == route.tenant_id,
            ProviderInbound.provider_event_id == provider_event_id,
        ))
        if winner:
            return {"accepted": True, "inbound_id": winner.id, "duplicate": True,
                "disposition": winner.disposition}
        raise
    return {"accepted": True, "inbound_id": item.id, "duplicate": False,
        "disposition": item.disposition, "route_id": route.id}


@router.post("/messages/{message_id}/tracking/{kind}", status_code=201)
def create_tracking_token(message_id: str, kind: str, ctx=Depends(auth), s: Session = Depends(db)):
    kind = kind.upper()
    if kind not in {"OPEN", "CLICK"}:
        raise HTTPException(422, "invalid_tracking_kind")
    message = s.scalar(select(ProviderMessage).where(ProviderMessage.id == message_id,
        ProviderMessage.tenant_id == ctx["tenant"]))
    if not message:
        raise HTTPException(404, "message_not_found")
    tenant_policy = policy_for(s, ctx["tenant"])
    if tenant_policy.tracking_mode == "DISABLED" or kind not in tenant_policy.tracking_mode:
        raise HTTPException(403, "tracking_not_enabled")
    raw = secrets.token_urlsafe(32)
    item = TrackingToken(id=str(uuid.uuid4()), token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        tenant_id=ctx["tenant"], message_id=message.id, kind=kind,
        expires_at=now()+timedelta(days=int(os.getenv("KLYROW_TRACKING_RETENTION_DAYS", "30"))))
    s.add(item)
    audit_provider(s, ctx, "tracking.token.created", "accepted", resource_id=item.id)
    s.commit()
    return {"token": raw, "kind": kind, "expires_at": item.expires_at.isoformat()}


@status_router.get("/t/{kind}/{token}", status_code=204)
def consume_tracking_token(kind: str, token: str, s: Session = Depends(db)):
    kind = kind.upper()
    if kind not in {"OPEN", "CLICK"} or len(token) < 32 or len(token) > 200:
        raise HTTPException(404, "tracking_token_not_found")
    item = s.scalar(select(TrackingToken).where(
        TrackingToken.token_hash == hashlib.sha256(token.encode()).hexdigest(), TrackingToken.kind == kind))
    expires_at = item.expires_at.replace(tzinfo=timezone.utc) if item and item.expires_at.tzinfo is None else (item.expires_at if item else None)
    if not item or expires_at < now():
        raise HTTPException(404, "tracking_token_not_found")
    if item.first_seen_at is None:
        item.first_seen_at = now()
        event_id = str(uuid.uuid4())
        s.add(ProviderEvent(id=event_id, tenant_id=item.tenant_id, message_id=item.message_id,
            kind="message."+kind.lower()+"ed", payload_json=json.dumps({"event_id": event_id,
            "event": "message."+kind.lower()+"ed", "message_id": item.message_id}, separators=(",", ":"), sort_keys=True)))
        s.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/operations/health")
def operations_health(ctx=Depends(auth), s: Session = Depends(db)):
    from .provider_outbox_delivery import status as outbox_status
    queued = s.scalar(select(func.count(ProviderMessage.id)).where(ProviderMessage.status == "QUEUED")) or 0
    dead = s.scalar(select(func.count(ProviderMessage.id)).where(ProviderMessage.status == "DEAD_LETTER")) or 0
    return {"status": "ok", "queue_depth": queued, "dead_letter": dead, "safe_mode": True, "live_delivery": False,
            "outbox": outbox_status(s, ctx["tenant"])}


@router.post("/operations/messages/{message_id}/retry")
def operation_message_retry(message_id: str, provider_absence_confirmed: bool = False, ctx=Depends(auth), s: Session = Depends(db)):
    if ctx.get("role") != "platform_admin" or not ctx.get("service"):
        raise HTTPException(403, "operator_authorization_required")
    message = s.scalar(select(ProviderMessage).where(ProviderMessage.id == message_id,
        ProviderMessage.tenant_id == ctx["tenant"]))
    if not message:
        raise HTTPException(404, "message_not_found")
    from .provider_outcome import reconcile_before_retry
    if message.status == "INDETERMINATE" and not reconcile_before_retry(state=message.status, provider_message_id=message.provider_message_id, provider_absence_confirmed=provider_absence_confirmed):
        raise HTTPException(409, "provider_reconciliation_required")
    if message.status not in {"FAILED", "DEAD_LETTER", "DEFERRED", "INDETERMINATE"} or not message.sandbox:
        raise HTTPException(409, "message_not_retryable")
    message.status = "QUEUED"
    message.available_at = now()
    message.lease_expires_at = None
    message.last_error = None
    audit_provider(s, ctx, "message.retry_requested", "accepted", resource_id=message.id)
    s.commit()
    return {"message_id": message.id, "status": message.status}


@router.post("/operations/events/{event_id}/retry")
def operation_event_retry(event_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    if ctx.get("role") != "platform_admin" or not ctx.get("service"):
        raise HTTPException(403, "operator_authorization_required")
    event = s.scalar(select(ProviderEvent).where(ProviderEvent.id == event_id,
        ProviderEvent.tenant_id == ctx["tenant"]))
    if not event:
        raise HTTPException(404, "event_not_found")
    if event.state not in {"RETRY", "DEAD_LETTER"}:
        raise HTTPException(409, "event_not_retryable")
    event.state = "RETRY"
    event.attempts = 0
    event.available_at = now()
    event.last_error = None
    audit_provider(s, ctx, "event.retry_requested", "accepted", resource_id=event.id)
    s.commit()
    return {"event_id": event.id, "state": event.state}


@router.post("/operations/usage/{usage_event_id}/retry")
def operation_usage_retry(usage_event_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    if ctx.get("role") != "platform_admin" or not ctx.get("service"):
        raise HTTPException(403, "operator_authorization_required")
    event = s.scalar(select(ProviderUsageEvent).where(
        ProviderUsageEvent.id == usage_event_id,
        ProviderUsageEvent.tenant_id == ctx["tenant"],
    ))
    if not event:
        raise HTTPException(404, "usage_event_not_found")
    if event.state not in {"RETRY", "DEAD_LETTER"}:
        raise HTTPException(409, "usage_event_not_retryable")
    event.state = "RETRY"
    event.attempts = 0
    event.available_at = now()
    event.last_error = None
    audit_provider(s, ctx, "usage_event.retry_requested", "accepted", resource_id=event.id)
    s.commit()
    return {"usage_event_id": event.id, "state": event.state}


@router.post("/operations/outbox/reconcile")
def operation_outbox_reconcile(payload: ProviderOutboxReconcileIn, ctx=Depends(auth), s: Session = Depends(db)):
    if ctx.get("role") != "platform_admin" or not ctx.get("service"):
        raise HTTPException(403, "operator_authorization_required")
    result = reconcile_provider_outbox_dead_letters(
        s, tenant_id=ctx["tenant"], limit=payload.limit, apply=payload.apply
    )
    if payload.apply:
        reason_hash = "sha256:" + hashlib.sha256(payload.reason.encode()).hexdigest()
        audit_provider(s, ctx, "provider_outbox.reconciled", "accepted", correlation_id=reason_hash)
        s.commit()
    return result


@router.post("/operations/reconcile")
def operation_reconcile(ctx=Depends(auth), s: Session = Depends(db)):
    if ctx.get("role") != "platform_admin" or not ctx.get("service"):
        raise HTTPException(403, "operator_authorization_required")
    messages = list(s.scalars(select(ProviderMessage).where(ProviderMessage.tenant_id == ctx["tenant"])).all())
    anomalies = []
    for message in messages:
        capture = s.scalar(select(SandboxCapture.id).where(SandboxCapture.message_id == message.id))
        usage = s.scalar(select(ProviderUsageEvent.id).where(ProviderUsageEvent.message_id == message.id))
        event = s.scalar(select(ProviderEvent.id).where(ProviderEvent.message_id == message.id,
            ProviderEvent.kind == "message.delivered"))
        if message.status == "DELIVERED" and not capture and message.sandbox:
            anomalies.append({"message_id": message.id, "kind": "missing_sandbox_capture"})
        if message.status == "DELIVERED" and not usage:
            anomalies.append({"message_id": message.id, "kind": "missing_usage_event"})
        if message.status == "DELIVERED" and not event:
            anomalies.append({"message_id": message.id, "kind": "missing_delivery_event"})
        if message.status in {"QUEUED", "PROCESSING", "DEFERRED"} and (now() - (message.updated_at.replace(tzinfo=timezone.utc) if message.updated_at.tzinfo is None else message.updated_at)).total_seconds() > 900:
            anomalies.append({"message_id": message.id, "kind": "stuck_message"})
    audit_provider(s, ctx, "mail.reconciled", "clean" if not anomalies else "anomalies", resource_id=str(len(anomalies)))
    s.commit()
    return {"tenant_id": ctx["tenant"], "messages_checked": len(messages), "anomalies": anomalies,
        "status": "PASS" if not anomalies else "ATTENTION_REQUIRED"}


@status_router.get("/healthz")
def provider_healthz():
    return {"status": "ok", "service": "klyrow-mail-provider"}


@status_router.get("/readyz")
def provider_readyz(s: Session = Depends(db)):
    s.execute(select(1))
    return {"status": "ready", "database": "ok", "safe_mode": os.getenv("KLYROW_SAFE_MODE", "true").lower() == "true"}


@status_router.get("/version")
def provider_version():
    return {"service": "klyrow-mail-provider", "version": os.getenv("KLYROW_RELEASE", "development"), "source_sha": os.getenv("KLYROW_SOURCE_SHA", "unknown")}
