"""Browser-bound OIDC, invitation-role, and send-capability security fixes.

The canonical production composition installs this module after the historical
browser extension routers are prepared and before their routes are copied onto
the application. It replaces only the affected endpoints; the underlying
PKCE, nonce, tenant, CSRF, idempotency, and delivery controls remain
authoritative in their existing modules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import UTC, timedelta
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .main import SECRET, MailIn, Tenant, User, audit, auth_rate, db, ph, sha
from . import (
    auth_bff,
    browser_auth_actions,
    invitation_flow,
    platform_owner,
    postal_provisioning,
    tenancy,
    tenancy_onboarding,
)
from .auth_bff import (
    BrowserSession,
    OidcLoginTransaction,
    browser_context,
    csrf_guard,
)
from .saas import Onboarding
from .tenancy import (
    ROLE_PERMISSIONS,
    OidcIdentity,
    TenantInvitation,
    TenantMember,
)

FLOW_COOKIE = "__Host-klyrow_oidc_flow"
FLOW_COOKIE_PATH = "/"
FLOW_COOKIE_VERSION = "v1"
FLOW_COOKIE_MAX_BINDINGS = 8
FLOW_BINDING_CONTEXT = b"klyrow-oidc-flow-v3\x00"
ACTION_MODES = frozenset({"update-password", "verify-email"})
browser_router = APIRouter(
    tags=["Browser flow security"],
    dependencies=[Depends(auth_bff._canonical_browser_request)],
)
legacy_router = APIRouter(tags=["Tenant authority"])
_INSTALLED = False


def _as_utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def flow_binding(state: str) -> str:
    """Bind one unpredictable OIDC state value to this Klyrow deployment."""

    return hmac.new(
        SECRET.encode(),
        FLOW_BINDING_CONTEXT + state.encode(),
        hashlib.sha256,
    ).hexdigest()


def authorization_state(url: str) -> str:
    """Return the one state parameter from a generated authorization URL."""

    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("state", [])
    if len(values) != 1 or not values[0]:
        raise HTTPException(503, "oidc_state_unavailable")
    return values[0]


def _valid_binding(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def parse_flow_cookie(raw: str) -> list[str]:
    """Return a bounded, deduplicated list of valid HMAC bindings.

    A single legacy binding remains readable during rollout; all newly emitted
    multi-flow cookies use the versioned format.
    """

    value = str(raw or "").strip().lower()
    if not value:
        return []
    if _valid_binding(value):
        return [value]
    prefix = FLOW_COOKIE_VERSION + "."
    if not value.startswith(prefix):
        return []

    result: list[str] = []
    for binding in value[len(prefix) :].split("."):
        if _valid_binding(binding) and binding not in result:
            result.append(binding)
    return result[-FLOW_COOKIE_MAX_BINDINGS:]


def encode_flow_cookie(bindings: list[str]) -> str:
    valid: list[str] = []
    for binding in bindings:
        normalized = str(binding or "").strip().lower()
        if _valid_binding(normalized) and normalized not in valid:
            valid.append(normalized)
    bounded = valid[-FLOW_COOKIE_MAX_BINDINGS:]
    if len(bounded) == 1:
        return bounded[0]
    return FLOW_COOKIE_VERSION + "." + ".".join(bounded)


def _set_flow_cookie_value(response, bindings: list[str]) -> None:
    response.set_cookie(
        FLOW_COOKIE,
        encode_flow_cookie(bindings),
        max_age=auth_bff._flow_ttl(),
        secure=True,
        httponly=True,
        samesite="lax",
        path=FLOW_COOKIE_PATH,
    )


def set_flow_cookie(response, request: Request, state: str) -> None:
    """Append one browser-flow binding while preserving bounded open tabs."""

    bindings = parse_flow_cookie(request.cookies.get(FLOW_COOKIE, ""))
    expected = flow_binding(state)
    bindings = [binding for binding in bindings if not hmac.compare_digest(binding, expected)]
    bindings.append(expected)
    _set_flow_cookie_value(response, bindings)


def clear_flow_cookie(response) -> None:
    response.delete_cookie(
        FLOW_COOKIE,
        secure=True,
        httponly=True,
        samesite="lax",
        path=FLOW_COOKIE_PATH,
    )


def remove_flow_cookie(response, request: Request, state: str) -> None:
    """Remove only the consumed transaction binding, retaining other tabs."""

    expected = flow_binding(state)
    remaining = [
        binding
        for binding in parse_flow_cookie(request.cookies.get(FLOW_COOKIE, ""))
        if not hmac.compare_digest(binding, expected)
    ]
    if remaining:
        _set_flow_cookie_value(response, remaining)
    else:
        clear_flow_cookie(response)


def require_flow_cookie(request: Request, state: str) -> None:
    expected = flow_binding(state)
    supplied = parse_flow_cookie(request.cookies.get(FLOW_COOKIE, ""))
    matched = False
    for binding in supplied:
        matched = hmac.compare_digest(binding, expected) or matched
    if not matched:
        raise HTTPException(
            403,
            "oidc_flow_cookie_mismatch",
            headers={"Cache-Control": "no-store"},
        )


def _redirect_with_flow(
    request: Request,
    url: str,
    *,
    status_code: int = 302,
):
    response = RedirectResponse(url, status_code=status_code)
    set_flow_cookie(response, request, authorization_state(url))
    response.headers["Cache-Control"] = "no-store"
    return response


def _json_with_flow(
    request: Request,
    payload: dict,
    redirect_to: str,
    *,
    status_code: int = 202,
) -> JSONResponse:
    response = JSONResponse(
        {**payload, "redirect_to": redirect_to},
        status_code=status_code,
    )
    set_flow_cookie(response, request, authorization_state(redirect_to))
    response.headers["Cache-Control"] = "no-store"
    return response


def _service_error_response(
    request: Request,
    state: str,
) -> RedirectResponse:
    response = RedirectResponse(
        "/service-error",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )
    remove_flow_cookie(response, request, state)
    return response


def _load_transaction(
    session: Session,
    state: str,
) -> OidcLoginTransaction:
    transaction = session.scalar(
        select(OidcLoginTransaction).where(OidcLoginTransaction.state_hash == sha(state))
    )
    if transaction is None or transaction.used_at is not None:
        raise HTTPException(
            410,
            "oidc_state_invalid_or_used",
            headers={"Cache-Control": "no-store"},
        )
    expiry = _as_utc(transaction.expires_at)
    if expiry is None or expiry <= auth_bff.now():
        raise HTTPException(
            410,
            "oidc_state_expired",
            headers={"Cache-Control": "no-store"},
        )
    return transaction


def _claim_transaction(
    session: Session,
    transaction: OidcLoginTransaction,
) -> None:
    """Claim state by compare-and-set without committing surrounding work."""

    current = auth_bff.now()
    statement = (
        update(OidcLoginTransaction)
        .where(
            OidcLoginTransaction.id == transaction.id,
            OidcLoginTransaction.used_at.is_(None),
            OidcLoginTransaction.expires_at > current,
        )
        .values(used_at=current)
    )
    result = session.execute(
        statement,
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(
            410,
            "oidc_state_invalid_or_used",
            headers={"Cache-Control": "no-store"},
        )


def _stage_new_session(
    session: Session,
    request: Request,
    identity: OidcIdentity,
    user: User,
    membership: TenantMember,
    tokens: dict,
    rotated_from_id: str | None = None,
) -> tuple[BrowserSession, str, str]:
    """Stage a browser session without crossing the callback commit boundary."""

    raw = secrets.token_urlsafe(48)
    csrf = auth_bff._inherited_csrf(session, rotated_from_id) or secrets.token_urlsafe(32)
    item = BrowserSession(
        id=str(uuid.uuid4()),
        token_hash=sha(raw),
        csrf_hash=sha(csrf),
        csrf_ciphertext=auth_bff._encrypt(csrf),
        identity_id=identity.id,
        user_id=user.id,
        tenant_id=membership.tenant_id,
        role=membership.role,
        refresh_ciphertext=(
            auth_bff._encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None
        ),
        id_token_ciphertext=(
            auth_bff._encrypt(tokens["id_token"]) if tokens.get("id_token") else None
        ),
        expires_at=auth_bff.now() + timedelta(seconds=auth_bff._session_ttl()),
        rotated_from_id=rotated_from_id,
        user_agent_hash=auth_bff._metadata_hash(request.headers.get("user-agent", "")),
        ip_hash=auth_bff._metadata_hash(request.client.host if request.client else ""),
    )
    session.add(item)
    return item, raw, csrf


def _stage_postal_provisioning(
    session: Session,
    tenant_id: str,
):
    """Stage the idempotent development-mode Postal job without committing."""

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant_not_found")
    mapping = session.get(
        postal_provisioning.PostalTenantMapping,
        tenant_id,
    )
    if mapping and mapping.state == postal_provisioning.READY:
        existing = session.scalar(
            select(postal_provisioning.PostalProvisioningOutbox)
            .where(postal_provisioning.PostalProvisioningOutbox.tenant_id == tenant_id)
            .order_by(postal_provisioning.PostalProvisioningOutbox.created_at.desc())
        )
        if existing is not None:
            return existing
    if mapping is None:
        mapping = postal_provisioning.PostalTenantMapping(
            tenant_id=tenant_id,
            state="PENDING",
            provider_mode="Development",
        )
        session.add(mapping)

    key = f"postal-tenant:{tenant_id}:v1"
    job = session.scalar(
        select(postal_provisioning.PostalProvisioningOutbox).where(
            postal_provisioning.PostalProvisioningOutbox.idempotency_key == key
        )
    )
    if job is None:
        job = postal_provisioning.PostalProvisioningOutbox(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            idempotency_key=key,
            state="PENDING",
            available_at=postal_provisioning.now(),
        )
        session.add(job)
    return job


def _resolve_identity_context_uncommitted(
    session: Session,
    claims: dict,
) -> tuple[OidcIdentity, User, TenantMember]:
    """Mirror canonical onboarding while retaining one callback transaction."""

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
    if identity is not None:
        user = session.get(User, identity.user_id)
        if user is None or not user.enabled:
            raise HTTPException(403, "account_disabled")
        tenancy_onboarding._profile(session, identity, claims)
        membership = None
        if identity.default_tenant_id:
            membership = session.scalar(
                select(TenantMember).where(
                    TenantMember.tenant_id == identity.default_tenant_id,
                    TenantMember.user_id == user.id,
                    TenantMember.active == True,
                )
            )
        if membership is None:
            membership = session.scalar(
                select(TenantMember)
                .where(
                    TenantMember.user_id == user.id,
                    TenantMember.active == True,
                )
                .order_by(TenantMember.created_at)
            )
        if membership is None:
            membership = tenancy_onboarding._starter_workspace(
                session,
                identity,
                user,
                claims,
            )
        tenant = session.get(Tenant, membership.tenant_id)
        if tenant is None or not tenant.enabled:
            raise HTTPException(403, "tenant_suspended")
        return identity, user, membership

    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise HTTPException(
            409,
            "verified_email_required_for_first_login",
        )
    if session.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "identity_link_review_required")

    placeholder_tenant = Tenant(
        id=str(uuid.uuid4()),
        name="Identity Bootstrap",
        quota=0,
        enabled=True,
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
    membership = tenancy_onboarding._invited_workspace(
        session,
        identity,
        user,
        claims,
    )
    if membership is None:
        membership = tenancy_onboarding._starter_workspace(
            session,
            identity,
            user,
            claims,
        )
    session.delete(placeholder_tenant)
    audit(
        session,
        {"tenant": membership.tenant_id, "sub": user.id},
        "identity.first_login",
    )
    return identity, user, membership


def _resolve_selected_identity_context_uncommitted(
    session: Session,
    claims: dict,
    invitation_id: str,
) -> tuple[OidcIdentity, User, TenantMember]:
    """Resolve an explicit invitation without an intermediate commit."""

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
    if identity is not None:
        user = session.get(User, identity.user_id)
        if user is None or not user.enabled:
            raise HTTPException(403, "account_disabled")
        tenancy_onboarding._profile(session, identity, claims)
        membership = accept_selected_invitation(
            session,
            identity,
            user,
            claims,
            invitation_id,
        )
        audit(
            session,
            {"tenant": membership.tenant_id, "sub": user.id},
            "identity.invitation_login",
        )
        return identity, user, membership

    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise HTTPException(
            409,
            "verified_email_required_for_first_login",
        )
    if session.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "identity_link_review_required")

    placeholder_tenant = Tenant(
        id=str(uuid.uuid4()),
        name="Identity Bootstrap",
        quota=0,
        enabled=True,
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
    membership = accept_selected_invitation(
        session,
        identity,
        user,
        claims,
        invitation_id,
    )
    session.delete(placeholder_tenant)
    audit(
        session,
        {"tenant": membership.tenant_id, "sub": user.id},
        "identity.first_login",
    )
    return identity, user, membership


def _begin_authorization(
    request: Request,
    session: Session,
    *,
    mode: str,
    return_to: str | None,
) -> RedirectResponse:
    url = auth_bff._authorization_url(
        request,
        session,
        mode,
        return_to,
    )
    return _redirect_with_flow(request, url)


@browser_router.get("/auth/login")
def login_start(
    request: Request,
    return_to: str | None = None,
    session: Session = Depends(db),
):
    return _begin_authorization(
        request,
        session,
        mode="login",
        return_to=return_to,
    )


@browser_router.get("/auth/signup")
def signup_start(
    request: Request,
    return_to: str | None = None,
    session: Session = Depends(db),
):
    return _begin_authorization(
        request,
        session,
        mode="signup",
        return_to=return_to,
    )


@browser_router.get("/auth/google")
def google_start(
    request: Request,
    return_to: str | None = None,
    session: Session = Depends(db),
):
    return _begin_authorization(
        request,
        session,
        mode="google",
        return_to=return_to,
    )


def _step_up_identity_context(
    session: Session,
    transaction: OidcLoginTransaction,
    claims: dict,
):
    old_session_id = transaction.mode.split(":", 1)[1]
    old_session = session.scalar(
        select(BrowserSession).where(BrowserSession.id == old_session_id).with_for_update()
    )
    if old_session is None or old_session.revoked_at is not None:
        raise HTTPException(401, "step_up_session_invalid")
    expiry = _as_utc(old_session.expires_at)
    if expiry is None or expiry <= auth_bff.now():
        raise HTTPException(401, "step_up_session_invalid")

    identity, user, membership = _resolve_identity_context_uncommitted(
        session,
        claims,
    )
    if (
        identity.id != old_session.identity_id
        or user.id != old_session.user_id
        or membership.tenant_id != old_session.tenant_id
    ):
        raise HTTPException(403, "step_up_identity_mismatch")
    return old_session, identity, user, membership


@browser_router.get("/auth/callback")
def oidc_callback(
    request: Request,
    state: str,
    code: str | None = None,
    kc_action_status: str | None = None,
    error: str | None = None,
    session: Session = Depends(db),
):
    transaction = _load_transaction(session, state)
    require_flow_cookie(request, state)

    if error or not code or (transaction.mode in ACTION_MODES and kc_action_status != "success"):
        try:
            _claim_transaction(session, transaction)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _service_error_response(request, state)

    verifier = auth_bff._decrypt(transaction.verifier_ciphertext)
    nonce = auth_bff._decrypt(transaction.nonce_ciphertext)
    tokens = auth_bff._exchange_code(code, verifier, request)
    claims = auth_bff._validate_id_token(tokens["id_token"], nonce)

    try:
        _claim_transaction(session, transaction)

        rotated_from_id: str | None = None
        old_session: BrowserSession | None = None
        if transaction.mode.startswith("invite:"):
            invitation_id = transaction.mode.split(":", 1)[1]
            identity, user, membership = _resolve_selected_identity_context_uncommitted(
                session,
                claims,
                invitation_id,
            )
        elif transaction.mode.startswith("step-up:"):
            old_session, identity, user, membership = _step_up_identity_context(
                session,
                transaction,
                claims,
            )
            rotated_from_id = old_session.id
        else:
            identity, user, membership = _resolve_identity_context_uncommitted(
                session,
                claims,
            )

        _stage_postal_provisioning(session, membership.tenant_id)
        _item, raw, _csrf = _stage_new_session(
            session,
            request,
            identity,
            user,
            membership,
            tokens,
            rotated_from_id=rotated_from_id,
        )
        if old_session is not None:
            old_session.revoked_at = auth_bff.now()
        session.commit()
    except Exception:
        session.rollback()
        raise

    response = RedirectResponse(
        auth_bff._safe_return_to(transaction.return_url),
        status_code=303,
    )
    auth_bff._set_session_cookie(response, raw)
    remove_flow_cookie(response, request, state)
    response.headers["Cache-Control"] = "no-store"
    return response


@browser_router.post("/auth/actions/recover", status_code=202)
def begin_recovery(
    payload: browser_auth_actions.RecoveryRequest,
    request: Request,
    session: Session = Depends(db),
):
    del payload
    auth_rate(request, "browser-recovery")
    redirect_to = browser_auth_actions._identity_action_url(
        request,
        session,
        mode="recover",
        return_to="/reset-success",
        forgot_credentials=True,
    )
    return _json_with_flow(
        request,
        {"status": "accepted"},
        redirect_to,
        status_code=202,
    )


@browser_router.post(
    "/auth/actions/update-password",
    status_code=202,
)
def begin_password_update(
    request: Request,
    session: Session = Depends(db),
):
    auth_rate(request, "browser-password-update")
    redirect_to = browser_auth_actions._identity_action_url(
        request,
        session,
        mode="update-password",
        return_to="/reset-success",
        action="UPDATE_PASSWORD",
    )
    return _json_with_flow(
        request,
        {"status": "accepted"},
        redirect_to,
        status_code=202,
    )


@browser_router.post("/auth/actions/verify-email", status_code=202)
def begin_email_verification(
    request: Request,
    session: Session = Depends(db),
):
    auth_rate(request, "browser-email-verification")
    redirect_to = browser_auth_actions._identity_action_url(
        request,
        session,
        mode="verify-email",
        return_to="/verification-success",
        action="VERIFY_EMAIL",
    )
    return _json_with_flow(
        request,
        {"status": "accepted"},
        redirect_to,
        status_code=202,
    )


@browser_router.post("/auth/actions/invitation")
def validate_invitation(
    payload: browser_auth_actions.InvitationValidationRequest,
    request: Request,
    session: Session = Depends(db),
):
    auth_rate(request, "browser-invitation")
    invitation = session.scalar(
        select(TenantInvitation).where(TenantInvitation.token_hash == sha(payload.token))
    )
    valid = False
    if invitation is not None and invitation.revoked_at is None and invitation.accepted_at is None:
        expiry = _as_utc(invitation.expires_at)
        valid = bool(expiry and expiry > auth_bff.now())
    if not valid:
        return JSONResponse(
            {"valid": False, "redirect_to": None},
            headers={"Cache-Control": "no-store"},
        )

    redirect_to = auth_bff._authorization_url(
        request,
        session,
        "signup",
        "/onboarding",
    )
    state = authorization_state(redirect_to)
    transaction = session.scalar(
        select(OidcLoginTransaction).where(OidcLoginTransaction.state_hash == sha(state))
    )
    if transaction is None:
        raise HTTPException(503, "oidc_invitation_state_unavailable")
    transaction.mode = "invite:" + invitation.id
    session.commit()
    return _json_with_flow(
        request,
        {"valid": True},
        redirect_to,
        status_code=200,
    )


@browser_router.get("/auth/step-up", include_in_schema=False)
def platform_owner_step_up(
    request: Request,
    return_to: str = "/admin",
    ctx: dict = Depends(browser_context),
    session: Session = Depends(db),
):
    session_id = str(ctx.get("sid") or "")
    if not session_id:
        raise HTTPException(401, "authentication_required")
    url = auth_bff._authorization_url(
        request,
        session,
        "step-up:" + session_id,
        auth_bff._safe_return_to(return_to, "/admin"),
    )
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend([("prompt", "login"), ("max_age", "0")])
    acr_values = os.getenv(
        "KLYROW_PLATFORM_OWNER_STEP_UP_ACR_VALUES",
        "2",
    ).strip()
    if acr_values:
        query.append(("acr_values", acr_values))
    redirect_to = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            "",
        )
    )
    return _redirect_with_flow(request, redirect_to)


def _role_has_permission(role: str, permission: str) -> bool:
    permissions = ROLE_PERMISSIONS.get(str(role or "").upper(), set())
    return "*" in permissions or permission in permissions


@browser_router.post("/app/api/email/send", status_code=202)
async def browser_send(
    payload: MailIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    session: Session = Depends(db),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
):
    if not _role_has_permission(ctx.get("role", ""), "mail.send"):
        raise HTTPException(403, "mail_send_permission_required")
    return await tenancy_onboarding._send(
        payload,
        ctx,
        session,
        idempotency_key,
    )


def _locked_valid_invitation(
    session: Session,
    *,
    invitation_id: str | None = None,
    token_hash: str | None = None,
    email: str | None = None,
) -> TenantInvitation:
    statement = select(TenantInvitation)
    if invitation_id is not None:
        statement = statement.where(TenantInvitation.id == invitation_id)
    elif token_hash is not None:
        statement = statement.where(TenantInvitation.token_hash == token_hash)
    else:
        raise RuntimeError("invitation selector required")
    invitation = session.scalar(statement.with_for_update())
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
    ):
        raise HTTPException(410, "invitation_invalid_or_expired")
    expiry = _as_utc(invitation.expires_at)
    if expiry is None or expiry <= auth_bff.now():
        raise HTTPException(410, "invitation_invalid_or_expired")
    if email is not None and invitation.email != email:
        raise HTTPException(410, "invitation_invalid_or_expired")
    tenant = session.get(Tenant, invitation.tenant_id)
    if tenant is None or not tenant.enabled:
        raise HTTPException(410, "invitation_tenant_unavailable")
    return invitation


def _membership_for_invitation(
    session: Session,
    invitation: TenantInvitation,
    user: User,
) -> TenantMember:
    membership = session.scalar(
        select(TenantMember)
        .where(
            TenantMember.tenant_id == invitation.tenant_id,
            TenantMember.user_id == user.id,
        )
        .with_for_update()
    )
    if membership is None:
        membership = TenantMember(
            id=str(uuid.uuid4()),
            tenant_id=invitation.tenant_id,
            user_id=user.id,
            role=invitation.role,
            active=True,
        )
        session.add(membership)
        return membership
    if membership.active and membership.role != invitation.role:
        raise HTTPException(
            409,
            "invitation_existing_member_role_change_denied",
        )
    if not membership.active:
        membership.role = invitation.role
        membership.active = True
    return membership


def accept_selected_invitation(
    session: Session,
    identity: OidcIdentity,
    user: User,
    claims: dict,
    invitation_id: str,
) -> TenantMember:
    if claims.get("email_verified") is not True:
        raise HTTPException(
            409,
            "verified_email_required_for_invitation",
        )
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            409,
            "verified_email_required_for_invitation",
        )
    invitation = _locked_valid_invitation(
        session,
        invitation_id=invitation_id,
        email=email,
    )
    membership = _membership_for_invitation(
        session,
        invitation,
        user,
    )
    invitation.accepted_at = tenancy_onboarding.now()
    identity.default_tenant_id = invitation.tenant_id
    user.tenant_id = invitation.tenant_id
    if session.get(Onboarding, invitation.tenant_id) is None:
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
            payload_json=json.dumps(
                {"invitation_id": invitation.id},
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    )
    return membership


@legacy_router.post("/v1/team/invitations/accept", status_code=201)
def accept_legacy_invitation(
    payload: tenancy.AcceptIn,
    session: Session = Depends(db),
):
    invitation = _locked_valid_invitation(
        session,
        token_hash=sha(payload.token),
    )
    user = session.scalar(select(User).where(User.email == invitation.email))
    if user is None or not user.enabled:
        raise HTTPException(409, "keycloak_user_link_required")
    membership = _membership_for_invitation(
        session,
        invitation,
        user,
    )
    invitation.accepted_at = auth_bff.now()
    audit(
        session,
        {"tenant": invitation.tenant_id, "sub": user.id},
        "tenant.invitation.accepted",
    )
    session.commit()
    return {
        "tenant_id": invitation.tenant_id,
        "role": membership.role,
        "user_id": user.id,
    }


def _remove_route(
    routes: list,
    *,
    path: str,
    method: str | None = None,
) -> int:
    removed = 0
    for route in list(routes):
        if getattr(route, "path", "") != path:
            continue
        methods = getattr(route, "methods", set()) or set()
        if method is not None and method not in methods:
            continue
        routes.remove(route)
        removed += 1
    return removed


def install_browser_security_fixes(app) -> None:
    """Replace only the endpoints owned by issue #83."""

    global _INSTALLED
    if _INSTALLED:
        return

    for path in ("/auth/login", "/auth/signup", "/auth/google"):
        _remove_route(auth_bff.router.routes, path=path, method="GET")
    _remove_route(
        browser_auth_actions.router.routes,
        path="/auth/callback",
        method="GET",
    )
    for path in (
        "/auth/actions/recover",
        "/auth/actions/update-password",
        "/auth/actions/verify-email",
        "/auth/actions/invitation",
    ):
        _remove_route(
            browser_auth_actions.router.routes,
            path=path,
            method="POST",
        )
    _remove_route(
        platform_owner.router.routes,
        path="/auth/step-up",
        method="GET",
    )
    _remove_route(
        tenancy_onboarding.router.routes,
        path="/app/api/email/send",
        method="POST",
    )
    _remove_route(
        app.router.routes,
        path="/v1/team/invitations/accept",
        method="POST",
    )

    auth_bff.FLOW_COOKIE = FLOW_COOKIE
    auth_bff._flow_binding = flow_binding
    auth_bff._authorization_state = authorization_state
    auth_bff._set_flow_cookie = set_flow_cookie
    auth_bff._clear_flow_cookie = clear_flow_cookie
    auth_bff._remove_flow_cookie = remove_flow_cookie
    auth_bff._require_flow_cookie = require_flow_cookie

    invitation_flow._accept_selected_invitation = accept_selected_invitation
    app.state.klyrow_browser_security_fixes_installed = True
    _INSTALLED = True


__all__ = [
    "FLOW_COOKIE",
    "FLOW_COOKIE_MAX_BINDINGS",
    "FLOW_COOKIE_PATH",
    "accept_selected_invitation",
    "authorization_state",
    "browser_router",
    "clear_flow_cookie",
    "encode_flow_cookie",
    "flow_binding",
    "install_browser_security_fixes",
    "legacy_router",
    "parse_flow_cookie",
    "remove_flow_cookie",
    "require_flow_cookie",
    "set_flow_cookie",
]
