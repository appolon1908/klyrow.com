"""Tenant/campaign isolated agent mailbox provisioning control plane.

This module reserves identities and validates local policy only. Provider, Odoo,
VICIdial and Keycloak adapters must attest their steps before activation.
"""

import json
import os
import re
import unicodedata
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base, Tenant, auth, db, require

router = APIRouter(prefix="/v1", tags=["agent-mailboxes"])


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid.uuid4())


class CampaignEmailDomain(Base):
    __tablename__ = "campaign_email_domains"
    __table_args__ = (
        UniqueConstraint("tenant_id", "campaign_id", name="uq_campaign_email_domain"),
        UniqueConstraint("tenant_id", "primary_domain", name="uq_campaign_primary_domain_owner"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    campaign_id: Mapped[str] = mapped_column(String, index=True)
    campaign_name: Mapped[str] = mapped_column(String)
    primary_domain: Mapped[str] = mapped_column(String)
    alias_domains: Mapped[str] = mapped_column(Text, default="[]")
    sender_domain_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    inbound_domain_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    sending_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    receiving_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    human_mailbox_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    domain_classification: Mapped[str] = mapped_column(String, default="SYSTEM_OR_SERVICE")
    default_reply_to: Mapped[str | None] = mapped_column(String, nullable=True)
    support_address: Mapped[str | None] = mapped_column(String, nullable=True)
    billing_address: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentMailbox(Base):
    __tablename__ = "agent_mailboxes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain", "local_part", name="uq_agent_mailbox_address"),
        UniqueConstraint(
            "tenant_id", "agent_id", "campaign_id", name="uq_agent_mailbox_assignment"
        ),
    )
    mailbox_id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    employee_id: Mapped[str | None] = mapped_column(String, nullable=True)
    keycloak_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    odoo_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vicidial_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    campaign_id: Mapped[str] = mapped_column(String, index=True)
    campaign_name: Mapped[str] = mapped_column(String)
    domain: Mapped[str] = mapped_column(String)
    local_part: Mapped[str] = mapped_column(String)
    primary_email: Mapped[str] = mapped_column(String, index=True)
    display_name: Mapped[str] = mapped_column(String)
    sending_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    receiving_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mailbox_status: Mapped[str] = mapped_column(String, default="PROVISIONING")
    quota: Mapped[int] = mapped_column(Integer, default=500)
    rate_limit: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_receive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provisioning_correlation_id: Mapped[str] = mapped_column(String, index=True)
    provisioning_error: Mapped[str | None] = mapped_column(String, nullable=True)
    audit_version: Mapped[int] = mapped_column(Integer, default=1)
    outbound_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    inbound_validated: Mapped[bool] = mapped_column(Boolean, default=False)


class MailboxAudit(Base):
    __tablename__ = "agent_mailbox_audit"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    mailbox_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    campaign_id: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    correlation_id: Mapped[str] = mapped_column(String, index=True)
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MailboxInboundRoute(Base):
    __tablename__ = "agent_mailbox_inbound_routes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "recipient", name="uq_agent_inbound_recipient"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    campaign_id: Mapped[str] = mapped_column(String, index=True)
    mailbox_id: Mapped[str] = mapped_column(String, index=True)
    recipient: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_route_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OutboundSenderAuthorization(Base):
    __tablename__ = "agent_outbound_sender_authorizations"
    __table_args__ = (UniqueConstraint("tenant_id", "sender", name="uq_agent_outbound_sender"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    campaign_id: Mapped[str] = mapped_column(String, index=True)
    mailbox_id: Mapped[str] = mapped_column(String, index=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    sender: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def normalize_first_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    local = re.sub(r"[^a-z0-9]", "", folded)
    if len(local) < 2:
        raise ValueError("first_name_local_part_too_short")
    return local


class DomainIn(BaseModel):
    campaign_id: str
    campaign_name: str
    primary_domain: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]+$")
    alias_domains: list[str] = []
    sender_domain_verified: bool = False
    inbound_domain_verified: bool = False
    sending_enabled: bool = False
    receiving_enabled: bool = False
    human_mailbox_enabled: bool = False
    domain_classification: str = Field(
        default="SYSTEM_OR_SERVICE", pattern="^(HUMAN_CAMPAIGN|SYSTEM_OR_SERVICE|ALIAS_ONLY)$"
    )
    default_reply_to: str | None = None
    support_address: str | None = None
    billing_address: str | None = None
    status: str = Field(default="pending", pattern="^(pending|active|suspended)$")


class ProvisionIn(BaseModel):
    event_id: str
    agent_id: str
    employee_id: str | None = None
    odoo_user_id: str | None = None
    vicidial_user_id: str | None = None
    keycloak_user_id: str | None = None
    campaign_id: str
    campaign_name: str
    first_name: str
    last_name: str
    display_name: str
    supervisor_id: str
    active: bool
    correlation_id: str
    approved_local_part: str | None = Field(default=None, pattern=r"^[a-z0-9]{2,64}$")
    quota: int = Field(default=500, ge=1, le=100000)
    rate_limit: int = Field(default=30, ge=1, le=1000)


class ValidationIn(BaseModel):
    outbound_validated: bool
    inbound_validated: bool


def mailbox_json(m):
    return {
        "mailbox_id": m.mailbox_id,
        "agent_id": m.agent_id,
        "campaign_id": m.campaign_id,
        "primary_email": m.primary_email,
        "display_name": m.display_name,
        "sending_enabled": m.sending_enabled,
        "receiving_enabled": m.receiving_enabled,
        "mailbox_status": m.mailbox_status,
        "quota": m.quota,
        "rate_limit": m.rate_limit,
        "failure_reason": m.provisioning_error,
    }


@router.post("/campaign-email-domains", status_code=201)
def domain_register(x: DomainIn, ctx=Depends(require("platform_admin")), s: Session = Depends(db)):
    existing = s.scalar(
        select(CampaignEmailDomain).where(
            CampaignEmailDomain.tenant_id == ctx["tenant"],
            CampaignEmailDomain.campaign_id == x.campaign_id,
        )
    )
    if existing:
        raise HTTPException(409, "campaign_domain_mapping_exists")
    if x.human_mailbox_enabled and x.domain_classification != "HUMAN_CAMPAIGN":
        raise HTTPException(422, "human_mailbox_requires_human_campaign_domain")
    approved = (
        x.status == "active"
        and x.sender_domain_verified
        and x.inbound_domain_verified
        and x.sending_enabled
        and x.receiving_enabled
    )
    values = x.model_dump()
    values["alias_domains"] = json.dumps(values["alias_domains"], separators=(",", ":"))
    row = CampaignEmailDomain(
        tenant_id=ctx["tenant"],
        **values,
        approved_by=ctx["sub"] if approved else None,
        approved_at=now() if approved else None,
    )
    s.add(row)
    s.commit()
    return {"id": row.id, "status": row.status, "activation_ready": approved}


@router.post("/agent-mailboxes/provision", status_code=202)
def provision(
    x: ProvisionIn, ctx=Depends(require("platform_admin", "tenant_admin")), s: Session = Depends(db)
):
    if not s.get(Tenant, ctx["tenant"]):
        raise HTTPException(403, "tenant_not_found")
    if (
        not all(
            [
                x.first_name.strip(),
                x.last_name.strip(),
                x.campaign_id.strip(),
                x.supervisor_id.strip(),
            ]
        )
        or not x.active
    ):
        raise HTTPException(422, "invalid_or_inactive_agent")
    replay = s.scalar(
        select(MailboxAudit).where(
            MailboxAudit.tenant_id == ctx["tenant"],
            MailboxAudit.action == "agent_mailbox.provision_requested",
            MailboxAudit.correlation_id == x.event_id,
        )
    )
    if replay and replay.mailbox_id:
        existing = s.get(AgentMailbox, replay.mailbox_id)
        return {**mailbox_json(existing), "already_existed": True}
    mapping = s.scalar(
        select(CampaignEmailDomain).where(
            CampaignEmailDomain.tenant_id == ctx["tenant"],
            CampaignEmailDomain.campaign_id == x.campaign_id,
        )
    )
    if not mapping or mapping.status != "active":
        raise HTTPException(409, "BLOCKED_DOMAIN_MAPPING_REQUIRED")
    if not all(
        [
            mapping.sender_domain_verified,
            mapping.inbound_domain_verified,
            mapping.sending_enabled,
            mapping.receiving_enabled,
        ]
    ):
        raise HTTPException(409, "UNVERIFIED_CAMPAIGN_DOMAIN")
    if not mapping.human_mailbox_enabled or mapping.domain_classification != "HUMAN_CAMPAIGN":
        raise HTTPException(409, "SYSTEM_DOMAIN_HUMAN_MAILBOX_DISABLED")
    try:
        local = normalize_first_name(x.first_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    collision = False
    occupied = set(
        s.scalars(
            select(AgentMailbox.local_part).where(
                AgentMailbox.tenant_id == ctx["tenant"],
                AgentMailbox.domain == mapping.primary_domain,
            )
        ).all()
    )
    if local in occupied:
        collision = True
        base = local + normalize_first_name(x.last_name)
        suggested = base
        suffix = 2
        while suggested in occupied:
            suggested = f"{base}{suffix}"
            suffix += 1
        if not x.approved_local_part:
            raise HTTPException(
                409,
                {
                    "code": "EMAIL_ADDRESS_CONFLICT",
                    "original_request": f"{local}@{mapping.primary_domain}",
                    "suggested_address": f"{suggested}@{mapping.primary_domain}",
                },
            )
        if ctx.get("role") != "platform_admin" or x.approved_local_part != suggested:
            raise HTTPException(403, "collision_resolution_requires_exact_admin_approval")
        local = suggested
    mailbox = AgentMailbox(
        tenant_id=ctx["tenant"],
        agent_id=x.agent_id,
        employee_id=x.employee_id,
        keycloak_user_id=x.keycloak_user_id,
        odoo_user_id=x.odoo_user_id,
        vicidial_user_id=x.vicidial_user_id,
        campaign_id=x.campaign_id,
        campaign_name=x.campaign_name,
        domain=mapping.primary_domain,
        local_part=local,
        primary_email=f"{local}@{mapping.primary_domain}",
        display_name=x.display_name,
        quota=x.quota,
        rate_limit=x.rate_limit,
        provisioning_correlation_id=x.correlation_id,
        mailbox_status="VALIDATION_PENDING",
    )
    s.add(mailbox)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, "EMAIL_ADDRESS_CONFLICT")
    s.add(
        MailboxInboundRoute(
            tenant_id=ctx["tenant"],
            campaign_id=x.campaign_id,
            mailbox_id=mailbox.mailbox_id,
            recipient=mailbox.primary_email,
            enabled=False,
        )
    )
    s.add(
        OutboundSenderAuthorization(
            tenant_id=ctx["tenant"],
            campaign_id=x.campaign_id,
            mailbox_id=mailbox.mailbox_id,
            agent_id=x.agent_id,
            sender=mailbox.primary_email,
            enabled=False,
        )
    )
    detail = {
        "catch_all": False,
        "credentials_exposed": False,
        "original_request": f"{normalize_first_name(x.first_name)}@{mapping.primary_domain}",
        "final_address": mailbox.primary_email,
        "collision_resolved": collision,
        "odoo_linked": bool(x.odoo_user_id),
    }
    s.add(
        MailboxAudit(
            tenant_id=ctx["tenant"],
            mailbox_id=mailbox.mailbox_id,
            agent_id=x.agent_id,
            campaign_id=x.campaign_id,
            action="agent_mailbox.provision_requested",
            correlation_id=x.event_id,
            detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
        )
    )
    s.commit()
    return {
        **mailbox_json(mailbox),
        "already_existed": False,
        "external_steps_required": [
            "keycloak",
            "klyrow_identity",
            "postal_inbound_route",
            "odoo_writeback",
            "vicidial_link",
        ],
    }


@router.post("/agent-mailboxes/{mailbox_id}/validate")
def validate(
    mailbox_id: str,
    x: ValidationIn,
    ctx=Depends(require("platform_admin")),
    s: Session = Depends(db),
):
    m = s.scalar(
        select(AgentMailbox).where(
            AgentMailbox.mailbox_id == mailbox_id, AgentMailbox.tenant_id == ctx["tenant"]
        )
    )
    if not m:
        raise HTTPException(404, "mailbox_not_found")
    m.outbound_validated = x.outbound_validated
    m.inbound_validated = x.inbound_validated
    if x.outbound_validated and x.inbound_validated and m.keycloak_user_id:
        m.mailbox_status = "ACTIVE"
        m.sending_enabled = True
        m.receiving_enabled = True
        m.activated_at = now()
        s.scalar(
            select(MailboxInboundRoute).where(MailboxInboundRoute.mailbox_id == m.mailbox_id)
        ).enabled = True
        s.scalar(
            select(OutboundSenderAuthorization).where(
                OutboundSenderAuthorization.mailbox_id == m.mailbox_id
            )
        ).enabled = True
    else:
        m.mailbox_status = "PROVISIONING_FAILED"
        m.sending_enabled = False
        m.receiving_enabled = False
        m.provisioning_error = "identity_or_route_validation_failed"
    m.audit_version += 1
    s.add(
        MailboxAudit(
            tenant_id=m.tenant_id,
            mailbox_id=m.mailbox_id,
            agent_id=m.agent_id,
            campaign_id=m.campaign_id,
            action="agent_mailbox." + m.mailbox_status.lower(),
            correlation_id=m.provisioning_correlation_id,
        )
    )
    s.commit()
    return mailbox_json(m)


@router.post("/agent-mailboxes/{mailbox_id}/suspend")
def suspend(
    mailbox_id: str,
    ctx=Depends(require("platform_admin", "tenant_admin")),
    s: Session = Depends(db),
):
    m = s.scalar(
        select(AgentMailbox).where(
            AgentMailbox.mailbox_id == mailbox_id, AgentMailbox.tenant_id == ctx["tenant"]
        )
    )
    if not m:
        raise HTTPException(404, "mailbox_not_found")
    m.sending_enabled = False
    m.receiving_enabled = False
    m.mailbox_status = "SUSPENDED"
    m.suspended_at = now()
    m.audit_version += 1
    route = s.scalar(
        select(MailboxInboundRoute).where(MailboxInboundRoute.mailbox_id == m.mailbox_id)
    )
    sender = s.scalar(
        select(OutboundSenderAuthorization).where(
            OutboundSenderAuthorization.mailbox_id == m.mailbox_id
        )
    )
    if route:
        route.enabled = False
    if sender:
        sender.enabled = False
    s.add(
        MailboxAudit(
            tenant_id=m.tenant_id,
            mailbox_id=m.mailbox_id,
            agent_id=m.agent_id,
            campaign_id=m.campaign_id,
            action="agent_mailbox.suspended",
            correlation_id=m.provisioning_correlation_id,
        )
    )
    s.commit()
    return mailbox_json(m)


@router.get("/agent-mailboxes")
def list_mailboxes(ctx=Depends(auth), s: Session = Depends(db)):
    q = select(AgentMailbox).where(AgentMailbox.tenant_id == ctx["tenant"])
    if ctx.get("role") == "codestra-email-agent":
        q = q.where(AgentMailbox.keycloak_user_id == ctx["sub"])
    return {"items": [mailbox_json(m) for m in s.scalars(q).all()]}


def authorize_agent_sender(
    s: Session, ctx: dict, sender: str, campaign_id: str | None, reply_to: str | None = None
):
    if not campaign_id:
        # Browser webmail has already enforced exact tenant, mailbox access,
        # enabled-sender and verified-domain policy before it reaches the
        # shared sending engine. Preserve campaign-required mode for every
        # other caller while allowing that explicitly marked server path.
        if ctx.get("browser") is True and ctx.get("_klyrow_mail_channel") == "webmail":
            return
        if (
            ctx.get("role") == "codestra-email-agent"
            or os.getenv("KLYROW_CAMPAIGN_REQUIRED", "false").lower() == "true"
        ):
            raise HTTPException(403, "campaign_required")
        return
    normalized = sender.lower()
    mapping = s.scalar(
        select(CampaignEmailDomain).where(
            CampaignEmailDomain.tenant_id == ctx["tenant"],
            CampaignEmailDomain.campaign_id == campaign_id,
            CampaignEmailDomain.status == "active",
            CampaignEmailDomain.sender_domain_verified == True,
            CampaignEmailDomain.sending_enabled == True,
        )
    )
    if not mapping or normalized.rsplit("@", 1)[-1] != mapping.primary_domain:
        raise HTTPException(403, "campaign_sender_domain_denied")
    if mapping.default_reply_to and (
        not reply_to or reply_to.lower() != mapping.default_reply_to.lower()
    ):
        raise HTTPException(403, "campaign_reply_to_denied")
    authorization = s.scalar(
        select(OutboundSenderAuthorization).where(
            OutboundSenderAuthorization.tenant_id == ctx["tenant"],
            OutboundSenderAuthorization.campaign_id == campaign_id,
            OutboundSenderAuthorization.sender == normalized,
            OutboundSenderAuthorization.enabled == True,
        )
    )
    if not authorization:
        raise HTTPException(403, "campaign_sender_not_authorized")
    if ctx.get("role") == "codestra-email-agent":
        m = s.scalar(
            select(AgentMailbox).where(
                AgentMailbox.mailbox_id == authorization.mailbox_id,
                AgentMailbox.keycloak_user_id == ctx["sub"],
                AgentMailbox.mailbox_status == "ACTIVE",
                AgentMailbox.sending_enabled == True,
            )
        )
        if not m:
            raise HTTPException(403, "agent_sender_not_authorized")
