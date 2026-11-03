"""Explicit invitation selection and one-time invitation delivery for browser users."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import UTC, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .main import Tenant, User, audit, db, ph
from . import auth_bff, tenancy_onboarding
from .auth_bff import BrowserSession, browser_context, csrf_guard
from .saas import Onboarding
from .tenancy import (
    OidcIdentity,
    TenantInvitation,
    TenantMember,
    assert_role_assignable,
    validate_role,
)

router = APIRouter(tags=["Browser invitations"])
_INSTALLED = False


def install_invitation_extensions() -> None:
    """Replace the browser invitation creator before routes are copied to the app."""

    global _INSTALLED
    if _INSTALLED:
        return
    for route in list(tenancy_onboarding.router.routes):
        if getattr(route, "path", "") == "/app/api/team/invitations" and "POST" in getattr(
            route, "methods", set()
        ):
            tenancy_onboarding.router.routes.remove(route)
    _INSTALLED = True


def _valid_selected_invitation(
    session: Session, claims: dict, invitation_id: str
) -> TenantInvitation:
    if claims.get("email_verified") is not True:
        raise HTTPException(409, "verified_email_required_for_invitation")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(409, "verified_email_required_for_invitation")
    invitation = session.get(TenantInvitation, invitation_id)
    if (
        invitation is None
        or invitation.email != email
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
    ):
        raise HTTPException(410, "invitation_invalid_or_expired")
    expiry = invitation.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= tenancy_onboarding.now():
        raise HTTPException(410, "invitation_invalid_or_expired")
    tenant = session.get(Tenant, invitation.tenant_id)
    if not tenant or not tenant.enabled:
        raise HTTPException(410, "invitation_tenant_unavailable")
    return invitation


def _accept_selected_invitation(
    session: Session,
    identity: OidcIdentity,
    user: User,
    claims: dict,
    invitation_id: str,
) -> TenantMember:
    invitation = _valid_selected_invitation(session, claims, invitation_id)
    membership = session.scalar(
        select(TenantMember).where(
            TenantMember.tenant_id == invitation.tenant_id,
            TenantMember.user_id == user.id,
        )
    )
    if membership is None:
        membership = TenantMember(
            id=str(uuid.uuid4()),
            tenant_id=invitation.tenant_id,
            user_id=user.id,
            role=invitation.role,
            active=True,
        )
    else:
        membership.role = invitation.role
        membership.active = True
    invitation.accepted_at = tenancy_onboarding.now()
    identity.default_tenant_id = invitation.tenant_id
    user.tenant_id = invitation.tenant_id
    session.add(membership)
    if not session.get(Onboarding, invitation.tenant_id):
        session.add(
            Onboarding(
                tenant_id=invitation.tenant_id,
                step=1,
                checklist_json='{"invitation":true}',
                completed=False,
            )
        )
    session.add(
        tenancy_onboarding.OnboardingEvent(
            id=str(uuid.uuid4()),
            identity_id=identity.id,
            tenant_id=invitation.tenant_id,
            event_type="invitation.accepted",
            payload_json='{"invitation_id":"' + invitation.id + '"}',
        )
    )
    return membership


def resolve_selected_identity_context(
    session: Session, claims: dict, invitation_id: str
) -> tuple[OidcIdentity, User, TenantMember]:
    """Resolve first/existing login against the exact validated invitation."""

    issuer = auth_bff._canonical_issuer()
    subject = str(claims.get("sub") or "")
    if not subject:
        raise HTTPException(401, "oidc_subject_missing")

    identity = session.scalar(
        select(OidcIdentity).where(
            OidcIdentity.issuer == issuer,
            OidcIdentity.subject == subject,
            OidcIdentity.enabled == True,
        )
    )
    if identity:
        user = session.get(User, identity.user_id)
        if not user or not user.enabled:
            raise HTTPException(403, "account_disabled")
        tenancy_onboarding._profile(session, identity, claims)
        membership = _accept_selected_invitation(session, identity, user, claims, invitation_id)
        audit(
            session,
            {"tenant": membership.tenant_id, "sub": user.id},
            "identity.invitation_login",
        )
        session.commit()
        return identity, user, membership

    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise HTTPException(409, "verified_email_required_for_first_login")
    if session.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "identity_link_review_required")

    placeholder_tenant = Tenant(
        id=str(uuid.uuid4()), name="Identity Bootstrap", quota=0, enabled=True
    )
    user = User(
        id=str(uuid.uuid4()),
        tenant_id=placeholder_tenant.id,
        email=email,
        password_hash=ph.hash(secrets.token_urlsafe(48)),
        role="tenant_user",
        enabled=True,
    )
    identity = OidcIdentity(
        id=str(uuid.uuid4()),
        issuer=issuer,
        subject=subject,
        user_id=user.id,
        default_tenant_id=None,
        identity_type="KLYROW_ONLY",
        enabled=True,
    )
    session.add_all([placeholder_tenant, user, identity])
    session.flush()
    tenancy_onboarding._profile(session, identity, claims)
    membership = _accept_selected_invitation(session, identity, user, claims, invitation_id)
    session.delete(placeholder_tenant)
    audit(
        session,
        {"tenant": membership.tenant_id, "sub": user.id},
        "identity.first_login",
    )
    session.commit()
    return identity, user, membership


@router.post("/app/api/team/invitations", status_code=201)
def browser_invite(
    payload: tenancy_onboarding.BrowserInviteIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    session: Session = Depends(db),
):
    """Return the production capability exactly once to the authorized creator."""

    tenancy_onboarding._require_management(ctx)
    role = validate_role(payload.role)
    assert_role_assignable(ctx["role"], role)
    raw = secrets.token_urlsafe(32)
    item = TenantInvitation(
        id=str(uuid.uuid4()),
        tenant_id=ctx["tenant"],
        email=payload.email.lower(),
        role=role,
        token_hash=auth_bff.sha(raw),
        expires_at=tenancy_onboarding.now() + timedelta(hours=payload.expires_hours),
        created_by=ctx["sub"],
    )
    session.add(item)
    audit(session, ctx, "tenant.invitation.created")
    session.commit()

    public_url = os.getenv("KLYROW_PUBLIC_URL", "https://app.klyrow.com").rstrip("/")
    result = {
        "id": item.id,
        "email": item.email,
        "role": item.role,
        "expires_at": item.expires_at.isoformat(),
        "invitation_url": f"{public_url}/invite?token={quote(raw, safe='')}",
        "delivery_method": "ONE_TIME_URL",
        "delivery_state": "READY_FOR_SECURE_SHARE",
    }
    return JSONResponse(
        result,
        status_code=201,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
