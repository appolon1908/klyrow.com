"""Safe browser account actions and hardened browser-session authority."""

from __future__ import annotations

import os
from datetime import UTC
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .main import Tenant, User, auth_rate, db, sha
from . import auth_bff
from .auth_bff import BrowserSession, OidcLoginTransaction
from .tenancy import OidcIdentity, TenantInvitation, TenantMember

router = APIRouter(tags=["Browser account actions"])
_ORIGINAL_NEW_SESSION = auth_bff._new_session
_ORIGINAL_GET_BROWSER_SESSION = auth_bff._get_browser_session
_INSTALLED = False
_ACTION_MODES = {"update-password", "verify-email"}


class RecoveryRequest(BaseModel):
    email: EmailStr


class InvitationValidationRequest(BaseModel):
    token: str = Field(min_length=16, max_length=512)


def _as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def browser_session_idle_seconds() -> int:
    """Return a bounded idle timeout that can never exceed the absolute TTL."""

    try:
        configured = int(os.getenv("KLYROW_BROWSER_SESSION_IDLE_SECONDS", "1800"))
    except ValueError as exc:
        raise RuntimeError("KLYROW_BROWSER_SESSION_IDLE_SECONDS must be an integer") from exc
    return max(300, min(configured, auth_bff._session_ttl()))


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _idle_timeout_seconds() -> int:
    """Return a bounded inactivity timeout that never exceeds the absolute TTL."""

    try:
        configured = int(os.getenv("KLYROW_BROWSER_SESSION_IDLE_SECONDS", "1800"))
    except (TypeError, ValueError):
        configured = 1800
    return max(300, min(configured, auth_bff._session_ttl()))


def _active_authority(
    session: Session,
    *,
    identity_id: str,
    user_id: str,
    tenant_id: str,
) -> tuple[OidcIdentity, User, TenantMember, Tenant]:
    """Reload every authority record that backs a browser session."""

    identity = session.get(OidcIdentity, identity_id, populate_existing=True)
    user = session.get(User, user_id, populate_existing=True)
    if (
        not identity
        or not identity.enabled
        or identity.user_id != user_id
        or not user
        or not user.enabled
    ):
        raise HTTPException(401, "principal_disabled")

    membership = session.scalar(
        select(TenantMember)
        .execution_options(populate_existing=True)
        .where(
            TenantMember.tenant_id == tenant_id,
            TenantMember.user_id == user_id,
            TenantMember.active == True,
        )
    )
    tenant = session.get(Tenant, tenant_id, populate_existing=True)
    if not membership or not tenant or not tenant.enabled:
        raise HTTPException(403, "workspace_access_denied")
    return identity, user, membership, tenant


def _get_browser_session_with_authority(
    request: Request,
    session: Session,
    touch: bool = True,
) -> BrowserSession:
    """Enforce absolute, idle, identity, membership and tenant validity."""

    item = _ORIGINAL_GET_BROWSER_SESSION(request, session, touch=False)
    current = auth_bff.now()
    last_seen = _aware(item.last_seen_at) or _aware(item.created_at) or current
    if (current - last_seen).total_seconds() > _idle_timeout_seconds():
        item.revoked_at = current
        session.commit()
        raise HTTPException(401, "session_idle_timeout")

    try:
        _identity, _user, membership, _tenant = _active_authority(
            session,
            identity_id=item.identity_id,
            user_id=item.user_id,
            tenant_id=item.tenant_id,
        )
    except HTTPException:
        item.revoked_at = current
        session.commit()
        raise

    changed = False
    if item.role != membership.role:
        item.role = membership.role
        changed = True
    if touch:
        item.last_seen_at = current
        changed = True
    if changed:
        session.commit()
    return item


def _new_session_with_parent_deadline(
    session: Session,
    request: Request,
    identity: OidcIdentity,
    user: User,
    membership: TenantMember,
    tokens: dict,
    rotated_from_id: str | None = None,
):
    """Create/rotate a session without extending its original absolute deadline.

    The random per-session CSRF token is minted (or inherited from the rotated
    parent) by ``auth_bff._new_session`` itself; nothing here derives it.
    """

    absolute_deadline = None
    if rotated_from_id:
        prior = session.get(BrowserSession, rotated_from_id, populate_existing=True)
        if not prior or prior.user_id != user.id or prior.identity_id != identity.id:
            raise HTTPException(401, "session_rotation_invalid")
        absolute_deadline = _aware(prior.expires_at)
        if not absolute_deadline or absolute_deadline <= auth_bff.now():
            raise HTTPException(401, "session_expired")

    identity, user, membership, _tenant = _active_authority(
        session,
        identity_id=identity.id,
        user_id=user.id,
        tenant_id=membership.tenant_id,
    )
    item, raw, csrf = _ORIGINAL_NEW_SESSION(
        session,
        request,
        identity,
        user,
        membership,
        tokens,
        rotated_from_id=rotated_from_id,
    )
    if absolute_deadline and _aware(item.expires_at) > absolute_deadline:
        item.expires_at = absolute_deadline
        session.commit()
    return item, raw, csrf


def _identity_action_url(
    request: Request,
    session: Session,
    *,
    mode: str,
    return_to: str,
    action: str | None = None,
    forgot_credentials: bool = False,
) -> str:
    url = auth_bff._authorization_url(request, session, mode, return_to)
    parsed = urlsplit(url)
    path = parsed.path
    if forgot_credentials:
        # Keycloak's supported client-initiated Reset Credentials flow replaces
        # only the terminal OIDC `/auth` segment with `/forgot-credentials`.
        suffix = "/protocol/openid-connect/auth"
        if not path.endswith(suffix):
            raise HTTPException(503, "canonical_oidc_misconfigured")
        path = path[: -len(suffix)] + "/protocol/openid-connect/forgot-credentials"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if action:
        query.append(("kc_action", action))
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def install_auth_extensions() -> None:
    """Replace historical routes and session helpers exactly once."""

    global _INSTALLED
    if _INSTALLED:
        return
    replacement_paths = {
        "/auth/session",
        "/auth/callback",
        "/auth/sessions",
        "/auth/sessions/{session_id}",
        "/auth/logout-all",
    }
    for route in list(auth_bff.router.routes):
        if getattr(route, "path", "") in replacement_paths:
            auth_bff.router.routes.remove(route)
    auth_bff._get_browser_session = _get_browser_session_with_authority
    auth_bff._new_session = _new_session_with_parent_deadline
    _INSTALLED = True


def current_browser_session(request: Request, session: Session = Depends(db)) -> BrowserSession:
    return auth_bff._get_browser_session(request, session)


@router.get("/auth/callback")
def oidc_callback(
    request: Request,
    state: str,
    code: str | None = None,
    kc_action_status: str | None = None,
    error: str | None = None,
    session: Session = Depends(db),
):
    transaction = session.scalar(
        select(OidcLoginTransaction).where(OidcLoginTransaction.state_hash == sha(state))
    )
    if not transaction or transaction.used_at:
        raise HTTPException(410, "oidc_state_invalid_or_used")
    expiry = _aware(transaction.expires_at)
    if not expiry or expiry <= auth_bff.now():
        raise HTTPException(410, "oidc_state_expired")
    transaction.used_at = auth_bff.now()
    session.commit()

    # Never turn a cancelled/omitted application-initiated action into success.
    if error or not code or (transaction.mode in _ACTION_MODES and kc_action_status != "success"):
        return RedirectResponse(
            "/service-error",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    verifier = auth_bff._decrypt(transaction.verifier_ciphertext)
    nonce = auth_bff._decrypt(transaction.nonce_ciphertext)
    tokens = auth_bff._exchange_code(code, verifier, request)
    claims = auth_bff._validate_id_token(tokens["id_token"], nonce)
    if transaction.mode.startswith("invite:"):
        from .invitation_flow import resolve_selected_identity_context
        from .postal_provisioning import enqueue_postal_provisioning

        invitation_id = transaction.mode.split(":", 1)[1]
        identity, user, membership = resolve_selected_identity_context(
            session, claims, invitation_id
        )
        enqueue_postal_provisioning(session, membership.tenant_id)
    else:
        identity, user, membership = auth_bff._identity_context(session, claims)
    _item, raw, _csrf = auth_bff._new_session(session, request, identity, user, membership, tokens)
    response = RedirectResponse(auth_bff._safe_return_to(transaction.return_url), status_code=303)
    auth_bff._set_session_cookie(response, raw)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/session")
def session_status(request: Request, session: Session = Depends(db)):
    try:
        item = auth_bff._get_browser_session(request, session)
    except HTTPException as exc:
        if exc.status_code == 401 and exc.detail not in {
            "principal_disabled",
            "session_idle_timeout",
        }:
            return JSONResponse(
                {"authenticated": False},
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        raise
    csrf = auth_bff._session_csrf(session, item)
    return JSONResponse(
        auth_bff._session_body(session, item, csrf),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/auth/sessions")
def list_user_sessions(
    current: BrowserSession = Depends(current_browser_session),
    session: Session = Depends(db),
):
    rows = session.scalars(
        select(BrowserSession)
        .where(BrowserSession.user_id == current.user_id)
        .order_by(BrowserSession.created_at.desc())
    ).all()
    return [
        {
            "id": row.id,
            "current": row.id == current.id,
            "identity_id": row.identity_id,
            "created_at": row.created_at,
            "last_seen_at": row.last_seen_at,
            "expires_at": row.expires_at,
            "revoked_at": row.revoked_at,
            "user_agent_hash": row.user_agent_hash,
            "ip_hash": row.ip_hash,
        }
        for row in rows
    ]


@router.delete("/auth/sessions/{session_id}", status_code=204)
def revoke_user_session(
    session_id: str,
    current: BrowserSession = Depends(auth_bff.csrf_guard),
    session: Session = Depends(db),
):
    item = session.get(BrowserSession, session_id)
    if not item or item.user_id != current.user_id:
        raise HTTPException(404, "session_not_found")
    item.revoked_at = auth_bff.now()
    session.commit()


@router.post("/auth/logout-all")
def logout_all_user_sessions(
    current: BrowserSession = Depends(auth_bff.csrf_guard),
    session: Session = Depends(db),
):
    rows = session.scalars(
        select(BrowserSession).where(
            BrowserSession.user_id == current.user_id,
            BrowserSession.revoked_at.is_(None),
        )
    ).all()
    revoked_at = auth_bff.now()
    for item in rows:
        item.revoked_at = revoked_at
    session.commit()
    response = JSONResponse(
        {"logged_out": True, "revoked_sessions": len(rows)},
        headers={"Cache-Control": "no-store"},
    )
    auth_bff._clear_session_cookie(response)
    return response


@router.post("/auth/actions/recover", status_code=202)
def begin_recovery(
    payload: RecoveryRequest,
    request: Request,
    session: Session = Depends(db),
):
    del payload
    auth_rate(request, "browser-recovery")
    return {
        "status": "accepted",
        "redirect_to": _identity_action_url(
            request,
            session,
            mode="recover",
            return_to="/reset-success",
            forgot_credentials=True,
        ),
    }


@router.post("/auth/actions/update-password", status_code=202)
def begin_password_update(request: Request, session: Session = Depends(db)):
    auth_rate(request, "browser-password-update")
    return {
        "status": "accepted",
        "redirect_to": _identity_action_url(
            request,
            session,
            mode="update-password",
            return_to="/reset-success",
            action="UPDATE_PASSWORD",
        ),
    }


@router.post("/auth/actions/verify-email", status_code=202)
def begin_email_verification(request: Request, session: Session = Depends(db)):
    auth_rate(request, "browser-email-verification")
    return {
        "status": "accepted",
        "redirect_to": _identity_action_url(
            request,
            session,
            mode="verify-email",
            return_to="/verification-success",
            action="VERIFY_EMAIL",
        ),
    }


@router.post("/auth/actions/invitation")
def validate_invitation(
    payload: InvitationValidationRequest,
    request: Request,
    session: Session = Depends(db),
):
    auth_rate(request, "browser-invitation")
    item = session.scalar(
        select(TenantInvitation).where(TenantInvitation.token_hash == sha(payload.token))
    )
    valid = False
    if item and not item.revoked_at and not item.accepted_at:
        expiry = _aware(item.expires_at)
        valid = bool(expiry and expiry > auth_bff.now())
    if not valid:
        return {"valid": False, "redirect_to": None}

    redirect_to = auth_bff._authorization_url(request, session, "signup", "/onboarding")
    state_values = parse_qs(urlsplit(redirect_to).query).get("state", [])
    if len(state_values) != 1:
        raise HTTPException(503, "oidc_invitation_state_unavailable")
    transaction = session.scalar(
        select(OidcLoginTransaction).where(OidcLoginTransaction.state_hash == sha(state_values[0]))
    )
    if transaction is None:
        raise HTTPException(503, "oidc_invitation_state_unavailable")
    transaction.mode = "invite:" + item.id
    session.commit()
    return {"valid": True, "redirect_to": redirect_to}
