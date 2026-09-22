"""Canonical identity onboarding and browser dashboard API."""
from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Boolean, DateTime, String, Text, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import auth_bff
from .auth_bff import BrowserSession, browser_context, csrf_guard
from .main import (
    AUTH_WEB_DIST,
    AllowedSender,
    Campaign,
    Contact,
    Domain,
    EmailOutbox,
    Event,
    MailIn,
    Message,
    Suppression,
    Tenant,
    User,
    WebhookEndpoint,
    _send,
    audit,
    db,
    ph,
)
from .saas import Onboarding, UsageLedger
from .tenancy import Organization, OidcIdentity, TenantInvitation, TenantMember, validate_role

router = APIRouter(tags=["Browser workspace"])
now = lambda: datetime.now(timezone.utc)


class IdentityProfile(BaseModel):
    email: Optional[str] = None
    email_verified: bool = False
    display_name: Optional[str] = None
    locale: Optional[str] = None


class IdentityProfileRecord(auth_bff.Base):
    __tablename__ = "identity_profiles"
    identity_id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    locale: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OnboardingEvent(auth_bff.Base):
    __tablename__ = "onboarding_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    identity_id: Mapped[str] = mapped_column(String, index=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OnboardingUpdate(BaseModel):
    step: int = Field(ge=1, le=12)
    use_case: Optional[str] = Field(default=None, max_length=80)
    checklist: dict[str, bool] = Field(default_factory=dict)


class BrowserInviteIn(BaseModel):
    email: EmailStr
    role: str = "READ_ONLY"
    expires_hours: int = Field(default=72, ge=1, le=720)


def _slug(s: Session, seed: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", seed.lower()).strip("-")[:42] or "workspace"
    candidate = base
    for _ in range(20):
        if not s.scalar(select(Organization).where(Organization.slug == candidate)):
            return candidate
        candidate = f"{base[:34]}-{secrets.token_hex(3)}"
    return f"workspace-{secrets.token_hex(6)}"


def _profile(s: Session, identity: OidcIdentity, claims: dict) -> IdentityProfileRecord:
    item = s.get(IdentityProfileRecord, identity.id) or IdentityProfileRecord(identity_id=identity.id)
    email = str(claims.get("email") or "").strip().lower() or None
    item.email = email
    item.email_verified = bool(claims.get("email_verified"))
    item.display_name = str(claims.get("name") or claims.get("preferred_username") or "").strip() or None
    item.locale = str(claims.get("locale") or "").strip()[:16] or None
    item.updated_at = now()
    s.add(item)
    return item


def _starter_workspace(s: Session, identity: OidcIdentity, user: User, claims: dict) -> TenantMember:
    display = str(claims.get("name") or "").strip()
    email = str(claims.get("email") or "").strip().lower()
    seed = display or (email.split("@", 1)[0] if email else "Klyrow Workspace")
    tenant = Tenant(id=str(uuid.uuid4()), name=f"{seed} Workspace", quota=1000)
    org = Organization(id=str(uuid.uuid4()), tenant_id=tenant.id, name=tenant.name, slug=_slug(s, seed))
    membership = TenantMember(id=str(uuid.uuid4()), tenant_id=tenant.id, user_id=user.id, role="OWNER")
    onboarding = Onboarding(tenant_id=tenant.id, step=1, checklist_json=json.dumps({"workspace": True}), completed=False)
    user.tenant_id = tenant.id
    user.role = "tenant_admin"
    identity.default_tenant_id = tenant.id
    s.add_all([tenant, org, membership, onboarding])
    s.add(OnboardingEvent(id=str(uuid.uuid4()), identity_id=identity.id, tenant_id=tenant.id, event_type="workspace.created", payload_json=json.dumps({"source": "first_login"})))
    return membership


def _invited_workspace(s: Session, identity: OidcIdentity, user: User, claims: dict) -> Optional[TenantMember]:
    if claims.get("email_verified") is not True:
        return None
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        return None
    candidates = s.scalars(
        select(TenantInvitation).where(
            TenantInvitation.email == email,
            TenantInvitation.accepted_at.is_(None),
            TenantInvitation.revoked_at.is_(None),
        ).order_by(TenantInvitation.created_at)
    ).all()
    valid = []
    for item in candidates:
        expiry = item.expires_at if item.expires_at.tzinfo else item.expires_at.replace(tzinfo=timezone.utc)
        if expiry > now():
            valid.append(item)
    if len(valid) != 1:
        return None
    invite = valid[0]
    membership = TenantMember(id=str(uuid.uuid4()), tenant_id=invite.tenant_id, user_id=user.id, role=invite.role, active=True)
    invite.accepted_at = now()
    identity.default_tenant_id = invite.tenant_id
    user.tenant_id = invite.tenant_id
    s.add(membership)
    if not s.get(Onboarding, invite.tenant_id):
        s.add(Onboarding(tenant_id=invite.tenant_id, step=1, checklist_json=json.dumps({"invitation": True}), completed=False))
    s.add(OnboardingEvent(id=str(uuid.uuid4()), identity_id=identity.id, tenant_id=invite.tenant_id, event_type="invitation.accepted", payload_json=json.dumps({"invitation_id": invite.id})))
    return membership


def resolve_identity_context(s: Session, claims: dict) -> tuple[OidcIdentity, User, TenantMember]:
    issuer = auth_bff._canonical_issuer()
    subject = str(claims.get("sub") or "")
    if not subject:
        raise HTTPException(401, "oidc_subject_missing")
    identity = s.scalar(select(OidcIdentity).where(OidcIdentity.issuer == issuer, OidcIdentity.subject == subject, OidcIdentity.enabled == True))
    if identity:
        user = s.get(User, identity.user_id)
        if not user or not user.enabled:
            raise HTTPException(403, "account_disabled")
        _profile(s, identity, claims)
        membership = None
        if identity.default_tenant_id:
            membership = s.scalar(select(TenantMember).where(TenantMember.tenant_id == identity.default_tenant_id, TenantMember.user_id == user.id, TenantMember.active == True))
        if not membership:
            membership = s.scalar(select(TenantMember).where(TenantMember.user_id == user.id, TenantMember.active == True).order_by(TenantMember.created_at))
        if not membership:
            membership = _starter_workspace(s, identity, user, claims)
        tenant = s.get(Tenant, membership.tenant_id)
        if not tenant or not tenant.enabled:
            raise HTTPException(403, "tenant_suspended")
        s.commit()
        return identity, user, membership

    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise HTTPException(409, "verified_email_required_for_first_login")
    if s.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "identity_link_review_required")
    placeholder_tenant = Tenant(id=str(uuid.uuid4()), name="Identity Bootstrap", quota=0, enabled=True)
    user = User(id=str(uuid.uuid4()), tenant_id=placeholder_tenant.id, email=email, password_hash=ph.hash(secrets.token_urlsafe(48)), role="tenant_user", enabled=True)
    identity = OidcIdentity(id=str(uuid.uuid4()), issuer=issuer, subject=subject, user_id=user.id, default_tenant_id=None, identity_type="KLYROW_ONLY", enabled=True)
    s.add_all([placeholder_tenant, user, identity])
    s.flush()
    _profile(s, identity, claims)
    membership = _invited_workspace(s, identity, user, claims)
    if not membership:
        membership = _starter_workspace(s, identity, user, claims)
    s.delete(placeholder_tenant)
    audit(s, {"tenant": membership.tenant_id, "sub": user.id}, "identity.first_login")
    s.commit()
    return identity, user, membership


def _organization_rows(s: Session, user_id: str) -> list[dict]:
    memberships = s.scalars(select(TenantMember).where(TenantMember.user_id == user_id, TenantMember.active == True).order_by(TenantMember.created_at)).all()
    result = []
    for membership in memberships:
        org = s.scalar(select(Organization).where(Organization.tenant_id == membership.tenant_id))
        tenant = s.get(Tenant, membership.tenant_id)
        if tenant and org:
            result.append({"tenant_id": tenant.id, "organization_id": org.id, "name": org.name, "slug": org.slug, "role": membership.role, "enabled": tenant.enabled})
    return result


def _require_management(ctx: dict) -> None:
    if ctx["role"] not in {"OWNER", "ADMIN"}:
        raise HTTPException(403, "tenant_management_denied")


@router.get("/app", include_in_schema=False)
@router.get("/onboarding", include_in_schema=False)
@router.get("/app/{path:path}", include_in_schema=False)
def product_app(path: str = ""):
    index = AUTH_WEB_DIST / "index.html"
    if not index.exists():
        raise HTTPException(503, "application_ui_not_built")
    return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-store"})


@router.get("/app/api/context")
def context(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    profile = s.get(IdentityProfileRecord, ctx["identity_id"])
    return {
        **ctx,
        "profile": IdentityProfile(email=profile.email, email_verified=profile.email_verified, display_name=profile.display_name, locale=profile.locale).model_dump() if profile else None,
        "organizations": _organization_rows(s, ctx["sub"]),
    }


@router.get("/app/api/dashboard")
def dashboard(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    tenant_id = ctx["tenant"]
    since = now() - timedelta(hours=24)
    messages_24h = s.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == tenant_id, Message.created_at >= since)) or 0
    messages_total = s.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == tenant_id)) or 0
    delivered = s.scalar(select(func.count()).select_from(Event).where(Event.tenant_id == tenant_id, Event.kind.in_(("email.delivered", "klyrow.email.delivered")))) or 0
    bounced = s.scalar(select(func.count()).select_from(Event).where(Event.tenant_id == tenant_id, Event.kind.in_(("email.bounced", "klyrow.email.bounced")))) or 0
    domains = s.scalars(select(Domain).where(Domain.tenant_id == tenant_id).order_by(Domain.domain)).all()
    senders = s.scalars(select(AllowedSender).where(AllowedSender.tenant_id == tenant_id, AllowedSender.enabled == True).order_by(AllowedSender.address)).all()
    recent = s.scalars(select(Message).where(Message.tenant_id == tenant_id).order_by(Message.created_at.desc()).limit(10)).all()
    onboarding = s.get(Onboarding, tenant_id)
    tenant = s.get(Tenant, tenant_id)
    queued = s.scalar(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.tenant_id == tenant_id, EmailOutbox.state.in_(("pending", "sending", "retry")))) or 0
    failed = s.scalar(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.tenant_id == tenant_id, EmailOutbox.state == "failed")) or 0
    contacts = s.scalar(select(func.count()).select_from(Contact).where(Contact.tenant_id == tenant_id)) or 0
    campaigns = s.scalar(select(func.count()).select_from(Campaign).where(Campaign.tenant_id == tenant_id)) or 0
    suppressions = s.scalar(select(func.count()).select_from(Suppression).where(Suppression.tenant_id == tenant_id)) or 0
    return {
        "metrics": {
            "sent_24h": messages_24h,
            "messages_total": messages_total,
            "quota": tenant.quota if tenant else 0,
            "delivered": delivered,
            "bounced": bounced,
            "delivery_rate": round(delivered / messages_total, 4) if messages_total else 0,
            "contacts": contacts,
            "campaigns": campaigns,
            "suppressions": suppressions,
            "outbox_active": queued,
            "outbox_failed": failed,
        },
        "domains": [{"id": item.id, "domain": item.domain, "verified": item.verified} for item in domains],
        "senders": [{"id": item.id, "address": item.address, "role": item.role} for item in senders],
        "recent_messages": [{"id": item.id, "recipient": item.recipient, "sender": item.sender, "subject": item.subject, "status": item.status, "created_at": item.created_at} for item in recent],
        "onboarding": {"step": onboarding.step, "use_case": onboarding.use_case, "checklist": json.loads(onboarding.checklist_json or "{}"), "completed": onboarding.completed} if onboarding else None,
    }


@router.get("/app/api/messages")
def browser_messages(ctx: dict = Depends(browser_context), s: Session = Depends(db), limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows = s.scalars(select(Message).where(Message.tenant_id == ctx["tenant"]).order_by(Message.created_at.desc()).offset(offset).limit(limit)).all()
    return [{"id": row.id, "recipient": row.recipient, "sender": row.sender, "subject": row.subject, "status": row.status, "created_at": row.created_at} for row in rows]


@router.post("/app/api/email/send", status_code=202)
async def browser_send(
    payload: MailIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    return await _send(payload, ctx, s, idempotency_key)


@router.get("/app/api/onboarding")
def onboarding_get(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    item = s.get(Onboarding, ctx["tenant"])
    if not item:
        item = Onboarding(tenant_id=ctx["tenant"], step=1, checklist_json="{}", completed=False)
        s.add(item)
        s.commit()
    return {"step": item.step, "use_case": item.use_case, "checklist": json.loads(item.checklist_json or "{}"), "completed": item.completed}


@router.patch("/app/api/onboarding")
def onboarding_update(payload: OnboardingUpdate, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    item = s.get(Onboarding, ctx["tenant"]) or Onboarding(tenant_id=ctx["tenant"], step=1, checklist_json="{}", completed=False)
    item.step = max(item.step or 1, payload.step)
    if payload.use_case:
        item.use_case = payload.use_case
    current = json.loads(item.checklist_json or "{}")
    current.update(payload.checklist)
    item.checklist_json = json.dumps(current)
    s.add(item)
    s.add(OnboardingEvent(id=str(uuid.uuid4()), identity_id=ctx["identity_id"], tenant_id=ctx["tenant"], event_type="onboarding.updated", payload_json=json.dumps({"step": item.step, "checklist": current})))
    s.commit()
    return {"step": item.step, "use_case": item.use_case, "checklist": current, "completed": item.completed}


@router.post("/app/api/onboarding/complete")
def onboarding_complete(ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    item = s.get(Onboarding, ctx["tenant"])
    if not item:
        raise HTTPException(409, "onboarding_not_started")
    item.completed = True
    item.step = max(item.step, 12)
    s.add(OnboardingEvent(id=str(uuid.uuid4()), identity_id=ctx["identity_id"], tenant_id=ctx["tenant"], event_type="onboarding.completed", payload_json="{}"))
    audit(s, ctx, "onboarding.completed")
    s.commit()
    return {"completed": True}


@router.post("/app/api/organizations/{tenant_id}/switch")
def browser_switch(tenant_id: str, request: Request, current: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    membership = s.scalar(select(TenantMember).where(TenantMember.tenant_id == tenant_id, TenantMember.user_id == current.user_id, TenantMember.active == True))
    tenant = s.get(Tenant, tenant_id)
    if not membership or not tenant or not tenant.enabled:
        raise HTTPException(404, "organization_not_found")
    identity = s.get(OidcIdentity, current.identity_id)
    user = s.get(User, current.user_id)
    if not identity or not user:
        raise HTTPException(401, "identity_unavailable")
    tokens = {}
    if current.refresh_ciphertext:
        tokens["refresh_token"] = auth_bff._decrypt(current.refresh_ciphertext)
    if current.id_token_ciphertext:
        tokens["id_token"] = auth_bff._decrypt(current.id_token_ciphertext)
    current.revoked_at = now()
    identity.default_tenant_id = tenant_id
    s.commit()
    item, raw, csrf = auth_bff._new_session(s, request, identity, user, membership, tokens, rotated_from_id=current.id)
    response = JSONResponse(auth_bff._session_body(s, item, csrf), headers={"Cache-Control": "no-store"})
    auth_bff._set_session_cookie(response, raw)
    return response


@router.get("/app/api/team")
def browser_team(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    rows = s.scalars(select(TenantMember).where(TenantMember.tenant_id == ctx["tenant"], TenantMember.active == True).order_by(TenantMember.created_at)).all()
    result = []
    for row in rows:
        user = s.get(User, row.user_id)
        result.append({"user_id": row.user_id, "email": user.email if user else None, "role": row.role, "created_at": row.created_at})
    return result


@router.post("/app/api/team/invitations", status_code=201)
def browser_invite(payload: BrowserInviteIn, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _require_management(ctx)
    role = validate_role(payload.role)
    raw = secrets.token_urlsafe(32)
    item = TenantInvitation(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], email=payload.email.lower(), role=role, token_hash=auth_bff.sha(raw), expires_at=now() + timedelta(hours=payload.expires_hours), created_by=ctx["sub"])
    s.add(item)
    audit(s, ctx, "tenant.invitation.created")
    s.commit()
    result = {"id": item.id, "email": item.email, "role": item.role, "expires_at": item.expires_at}
    if os.getenv("KLYROW_ENV", "development").lower() != "production":
        result["development_token"] = raw
    return result



@router.get("/app/api/team/invitations")
def browser_invitations(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    _require_management(ctx)
    rows = s.scalars(select(TenantInvitation).where(
        TenantInvitation.tenant_id == ctx["tenant"],
        TenantInvitation.accepted_at.is_(None),
        TenantInvitation.revoked_at.is_(None),
        TenantInvitation.expires_at > now(),
    ).order_by(TenantInvitation.created_at.desc())).all()
    return [{"id": row.id, "email": row.email, "role": row.role, "expires_at": row.expires_at, "created_at": row.created_at} for row in rows]


@router.delete("/app/api/team/invitations/{invitation_id}", status_code=204)
def browser_revoke_invitation(invitation_id: str, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _require_management(ctx)
    item = s.scalar(select(TenantInvitation).where(TenantInvitation.id == invitation_id, TenantInvitation.tenant_id == ctx["tenant"]))
    if not item or item.accepted_at is not None or item.revoked_at is not None:
        raise HTTPException(404, "invitation_not_found")
    item.revoked_at = now()
    audit(s, ctx, "tenant.invitation.revoked")
    s.commit()
    return Response(status_code=204)


class BrowserMemberRoleIn(BaseModel):
    role: str


def _active_owner_count(s: Session, tenant_id: str) -> int:
    return int(s.scalar(select(func.count()).select_from(TenantMember).where(
        TenantMember.tenant_id == tenant_id, TenantMember.active == True, TenantMember.role == "OWNER"
    )) or 0)


@router.patch("/app/api/team/{user_id}")
def browser_update_member(user_id: str, payload: BrowserMemberRoleIn, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _require_management(ctx)
    member = s.scalar(select(TenantMember).where(TenantMember.tenant_id == ctx["tenant"], TenantMember.user_id == user_id, TenantMember.active == True))
    if not member:
        raise HTTPException(404, "member_not_found")
    role = validate_role(payload.role)
    if member.role == "OWNER" and role != "OWNER" and _active_owner_count(s, ctx["tenant"]) <= 1:
        raise HTTPException(409, "last_owner_protected")
    member.role = role
    audit(s, ctx, "tenant.member.role_changed")
    s.commit()
    return {"user_id": member.user_id, "role": member.role, "active": member.active}


@router.delete("/app/api/team/{user_id}", status_code=204)
def browser_remove_member(user_id: str, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _require_management(ctx)
    member = s.scalar(select(TenantMember).where(TenantMember.tenant_id == ctx["tenant"], TenantMember.user_id == user_id, TenantMember.active == True))
    if not member:
        raise HTTPException(404, "member_not_found")
    if member.role == "OWNER" and _active_owner_count(s, ctx["tenant"]) <= 1:
        raise HTTPException(409, "last_owner_protected")
    member.active = False
    audit(s, ctx, "tenant.member.removed")
    s.commit()
    return Response(status_code=204)


@router.get("/app/api/identity/capabilities")
def browser_identity_capabilities(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    _require_management(ctx)
    return {
        "identity_authority": "Keycloak",
        "browser_session_authority": "Klyrow",
        "sso": {"configured": False, "mutation_available": False, "dependency": "governed Keycloak/Middleware provisioning contract"},
        "scim": {"configured": False, "mutation_available": False, "dependency": "governed Keycloak/Middleware provisioning contract"},
        "runtime_certification": "blocked" if os.getenv("KLYROW_IDENTITY_RUNTIME_CERTIFIED", "false").lower() != "true" else "certified",
        "direct_keycloak_writes": False,
    }


@router.get("/app/api/admin/dashboard")
def platform_dashboard(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    user = s.get(User, ctx["sub"])
    if not user or user.role != "platform_admin":
        raise HTTPException(403, "platform_admin_required")
    return {
        "tenants": s.scalar(select(func.count()).select_from(Tenant)) or 0,
        "users": s.scalar(select(func.count()).select_from(User)) or 0,
        "messages": s.scalar(select(func.count()).select_from(Message)) or 0,
        "outbox_active": s.scalar(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.state.in_(("pending", "sending", "retry")))) or 0,
        "outbox_failed": s.scalar(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.state == "failed")) or 0,
        "verified_domains": s.scalar(select(func.count()).select_from(Domain).where(Domain.verified == True)) or 0,
        "webhooks": s.scalar(select(func.count()).select_from(WebhookEndpoint)) or 0,
        "usage_events": s.scalar(select(func.count()).select_from(UsageLedger)) or 0,
    }
