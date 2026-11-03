"""Exact existing-identity resolver for browser platform-owner step-up.

A step-up transaction proves freshness for an already authenticated browser
session. It must never enter the first-login or provisioning paths when the
user selects another Keycloak identity at the prompt.
"""

from __future__ import annotations

from datetime import UTC

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .main import Tenant, User
from . import auth_bff, browser_security_fixes
from .auth_bff import BrowserSession, OidcLoginTransaction
from .tenancy import OidcIdentity, TenantMember


def _as_utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def resolve_existing_step_up_identity(
    session: Session,
    transaction: OidcLoginTransaction,
    claims: dict,
):
    """Resolve only the exact identity already bound to the browser session."""

    prefix, separator, old_session_id = transaction.mode.partition(":")
    if prefix != "step-up" or not separator or not old_session_id:
        raise HTTPException(401, "step_up_session_invalid")

    old_session = session.scalar(
        select(BrowserSession).where(BrowserSession.id == old_session_id).with_for_update()
    )
    expiry = _as_utc(old_session.expires_at) if old_session else None
    if (
        old_session is None
        or old_session.revoked_at is not None
        or expiry is None
        or expiry <= auth_bff.now()
    ):
        raise HTTPException(401, "step_up_session_invalid")

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise HTTPException(403, "step_up_identity_mismatch")
    issuer = auth_bff._canonical_issuer()
    identity = session.scalar(
        select(OidcIdentity)
        .where(
            OidcIdentity.issuer == issuer,
            OidcIdentity.subject == subject,
            OidcIdentity.enabled == True,
        )
        .with_for_update()
    )
    if (
        identity is None
        or identity.id != old_session.identity_id
        or identity.user_id != old_session.user_id
    ):
        raise HTTPException(403, "step_up_identity_mismatch")

    user = session.get(User, old_session.user_id)
    if user is None or not user.enabled:
        raise HTTPException(403, "step_up_account_disabled")

    membership = session.scalar(
        select(TenantMember)
        .where(
            TenantMember.tenant_id == old_session.tenant_id,
            TenantMember.user_id == old_session.user_id,
            TenantMember.active == True,
        )
        .with_for_update()
    )
    tenant = session.get(Tenant, old_session.tenant_id)
    if membership is None or tenant is None or not tenant.enabled:
        raise HTTPException(403, "step_up_workspace_invalid")

    return old_session, identity, user, membership


def install_existing_step_up_identity_guard(app) -> None:
    """Install the exact resolver into the canonical browser composition."""

    if getattr(
        app.state,
        "klyrow_existing_step_up_identity_guard_installed",
        False,
    ):
        return
    browser_security_fixes._step_up_identity_context = resolve_existing_step_up_identity
    app.state.klyrow_existing_step_up_identity_guard_installed = True


__all__ = [
    "install_existing_step_up_identity_guard",
    "resolve_existing_step_up_identity",
]
