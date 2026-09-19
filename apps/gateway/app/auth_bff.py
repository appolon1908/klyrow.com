"""Same-origin OIDC BFF and opaque browser-session authority for Klyrow."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from jwt import PyJWKClient
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base, SECRET, Tenant, User, db, sha
from .tenancy import ROLE_PERMISSIONS, OidcIdentity, TenantMember

SESSION_COOKIE = "__Host-klyrow_session"
from .identity_profile import canonical_issuer, identity_authority

ISSUER = "https://auth.codestra.co/realms/codestra"
CANONICAL_APP_ORIGIN = "https://app.klyrow.com"
_jwks_clients: dict[str, PyJWKClient] = {}
now = lambda: datetime.now(timezone.utc)


def _is_production() -> bool:
    return os.getenv("KLYROW_ENV", "development").lower() == "production"


def _public_origin() -> str:
    configured = os.getenv("KLYROW_PUBLIC_URL", identity_authority()[1]).strip().rstrip("/")
    parsed = urlsplit(configured)
    valid = (
        parsed.scheme in ({"https"} if _is_production() else {"http", "https"})
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )
    if not valid:
        raise HTTPException(503, "canonical_app_origin_misconfigured")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if _is_production() and origin != identity_authority()[1]:
        raise HTTPException(503, "canonical_app_origin_misconfigured")
    return origin


def _canonical_browser_request(request: Request) -> None:
    """Keep the browser/BFF surface on the one registered public origin."""
    if not _is_production():
        return
    expected_host = urlsplit(_public_origin()).hostname
    if request.url.hostname != expected_host:
        raise HTTPException(421, "canonical_app_host_required")


router = APIRouter(tags=["Browser authentication"], dependencies=[Depends(_canonical_browser_request)])


class OidcLoginTransaction(Base):
    __tablename__ = "oidc_login_transactions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    state_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    verifier_ciphertext: Mapped[str] = mapped_column(Text)
    nonce_ciphertext: Mapped[str] = mapped_column(Text)
    return_url: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String)
    identity_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)
    refresh_ciphertext: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    id_token_ciphertext: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rotated_from_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ip_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)


def _canonical_issuer() -> str:
    return canonical_issuer()


def _client_id() -> str:
    return os.getenv("KLYROW_OIDC_CLIENT_ID", "klyrow-portal")


def _client_secret() -> Optional[str]:
    path = os.getenv("KLYROW_OIDC_CLIENT_SECRET_FILE", "").strip()
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HTTPException(503, "oidc_client_secret_unavailable") from exc
    return value or None


def _redirect_uri(request: Request) -> str:
    configured = os.getenv("KLYROW_OIDC_REDIRECT_URI", "").strip()
    if not configured:
        configured = (_public_origin() if _is_production() else str(request.base_url).rstrip("/")) + "/auth/callback"
    parsed = urlsplit(configured)
    valid = (
        parsed.scheme in ({"https"} if _is_production() else {"http", "https"})
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/auth/callback"
        and not parsed.query
        and not parsed.fragment
    )
    if not valid:
        raise HTTPException(503, "oidc_redirect_uri_misconfigured")
    if _is_production() and configured != identity_authority()[1] + "/auth/callback":
        raise HTTPException(503, "oidc_redirect_uri_misconfigured")
    return configured


def _session_ttl() -> int:
    return max(300, min(int(os.getenv("KLYROW_BROWSER_SESSION_TTL_SECONDS", "28800")), 86400))


def _flow_ttl() -> int:
    return max(60, min(int(os.getenv("KLYROW_OIDC_FLOW_TTL_SECONDS", "600")), 1800))


def _key() -> bytes:
    return hashlib.sha256(("klyrow-bff:" + SECRET).encode()).digest()


def _encrypt(value: str) -> str:
    nonce = secrets.token_bytes(12)
    payload = nonce + AESGCM(_key()).encrypt(nonce, value.encode(), b"klyrow-bff-v1")
    return base64.urlsafe_b64encode(payload).decode()


def _decrypt(value: str) -> str:
    payload = base64.urlsafe_b64decode(value.encode())
    return AESGCM(_key()).decrypt(payload[:12], payload[12:], b"klyrow-bff-v1").decode()


def _metadata_hash(value: str) -> Optional[str]:
    if not value:
        return None
    return hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def _safe_return_to(value: Optional[str], default: str = "/app") -> str:
    candidate = value or default
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate or "\r" in candidate or "\n" in candidate:
        return default
    return candidate


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _authorization_url(request: Request, s: Session, mode: str, return_to: Optional[str]) -> str:
    redirect_uri = _redirect_uri(request)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    tx = OidcLoginTransaction(
        id=str(uuid.uuid4()),
        state_hash=sha(state),
        verifier_ciphertext=_encrypt(verifier),
        nonce_ciphertext=_encrypt(nonce),
        return_url=_safe_return_to(return_to, "/onboarding" if mode == "signup" else "/app"),
        mode=mode,
        expires_at=now() + timedelta(seconds=_flow_ttl()),
    )
    s.add(tx)
    s.commit()
    issuer = _canonical_issuer()
    endpoint = issuer + ("/protocol/openid-connect/registrations" if mode == "signup" else "/protocol/openid-connect/auth")
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if mode == "google":
        params["kc_idp_hint"] = "google"
    return endpoint + "?" + urlencode(params)


def _post_oidc_token(data: dict) -> dict:
    response = httpx.post(
        _canonical_issuer() + "/protocol/openid-connect/token", data=data,
        timeout=8, follow_redirects=False, trust_env=False,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("oidc_token_response_must_be_an_object")
    return payload


def _exchange_code(code: str, verifier: str, request: Request) -> dict:
    data = {
        "grant_type": "authorization_code",
        "client_id": _client_id(),
        "code": code,
        "redirect_uri": _redirect_uri(request),
        "code_verifier": verifier,
    }
    secret = _client_secret()
    if secret:
        data["client_secret"] = secret
    try:
        payload = _post_oidc_token(data)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "oidc_token_exchange_failed") from exc
    if not payload.get("id_token"):
        raise HTTPException(502, "oidc_id_token_missing")
    return payload


def _validate_id_token(raw: str, expected_nonce: Optional[str] = None) -> dict:
    issuer = _canonical_issuer()
    try:
        client = _jwks_clients.setdefault(issuer, PyJWKClient(issuer + "/protocol/openid-connect/certs", cache_keys=True, lifespan=300))
        signing_key = client.get_signing_key_from_jwt(raw)
        claims = jwt.decode(
            raw,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=_client_id(),
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except Exception as exc:
        raise HTTPException(401, "invalid_oidc_id_token") from exc
    azp = claims.get("azp")
    if azp and azp != _client_id():
        raise HTTPException(401, "invalid_oidc_authorized_party")
    if expected_nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), expected_nonce):
        raise HTTPException(401, "invalid_oidc_nonce")
    return claims


def _identity_context(s: Session, claims: dict) -> tuple[OidcIdentity, User, TenantMember]:
    identity = s.scalar(
        select(OidcIdentity).where(
            OidcIdentity.issuer == _canonical_issuer(),
            OidcIdentity.subject == str(claims["sub"]),
            OidcIdentity.enabled == True,
        )
    )
    if not identity:
        raise HTTPException(409, "identity_onboarding_required")
    user = s.get(User, identity.user_id)
    if not user or not user.enabled:
        raise HTTPException(403, "account_disabled")
    membership = None
    if identity.default_tenant_id:
        membership = s.scalar(
            select(TenantMember).where(
                TenantMember.tenant_id == identity.default_tenant_id,
                TenantMember.user_id == user.id,
                TenantMember.active == True,
            )
        )
    if not membership:
        membership = s.scalar(
            select(TenantMember).where(TenantMember.user_id == user.id, TenantMember.active == True).order_by(TenantMember.created_at)
        )
    if not membership:
        raise HTTPException(409, "workspace_onboarding_required")
    tenant = s.get(Tenant, membership.tenant_id)
    if not tenant or not tenant.enabled:
        raise HTTPException(403, "tenant_suspended")
    return identity, user, membership


def _new_session(
    s: Session,
    request: Request,
    identity: OidcIdentity,
    user: User,
    membership: TenantMember,
    tokens: dict,
    rotated_from_id: Optional[str] = None,
) -> tuple[BrowserSession, str, str]:
    raw = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    item = BrowserSession(
        id=str(uuid.uuid4()),
        token_hash=sha(raw),
        csrf_hash=sha(csrf),
        identity_id=identity.id,
        user_id=user.id,
        tenant_id=membership.tenant_id,
        role=membership.role,
        refresh_ciphertext=_encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
        id_token_ciphertext=_encrypt(tokens["id_token"]) if tokens.get("id_token") else None,
        expires_at=now() + timedelta(seconds=_session_ttl()),
        rotated_from_id=rotated_from_id,
        user_agent_hash=_metadata_hash(request.headers.get("user-agent", "")),
        ip_hash=_metadata_hash(request.client.host if request.client else ""),
    )
    s.add(item)
    s.commit()
    return item, raw, csrf


def _set_session_cookie(response, raw: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=_session_ttl(),
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax", path="/")


def _get_browser_session(request: Request, s: Session, touch: bool = True) -> BrowserSession:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw:
        raise HTTPException(401, "authentication_required")
    item = s.scalar(select(BrowserSession).where(BrowserSession.token_hash == sha(raw)))
    if not item or item.revoked_at:
        raise HTTPException(401, "session_revoked")
    expiry = item.expires_at if item.expires_at.tzinfo else item.expires_at.replace(tzinfo=timezone.utc)
    if expiry <= now():
        item.revoked_at = now()
        s.commit()
        raise HTTPException(401, "session_expired")
    if touch:
        item.last_seen_at = now()
        s.commit()
    return item


def browser_context(request: Request, s: Session = Depends(db)) -> dict:
    session = _get_browser_session(request, s)
    member = s.scalar(
        select(TenantMember).where(
            TenantMember.tenant_id == session.tenant_id,
            TenantMember.user_id == session.user_id,
            TenantMember.active == True,
        )
    )
    tenant = s.get(Tenant, session.tenant_id)
    if not member or not tenant or not tenant.enabled:
        raise HTTPException(403, "workspace_access_denied")
    return {
        "sub": session.user_id,
        "identity_id": session.identity_id,
        "tenant": session.tenant_id,
        "role": member.role,
        "sid": session.id,
        "browser": True,
    }


def csrf_guard(
    request: Request,
    x_klyrow_csrf: str = Header(default="", alias="X-Klyrow-CSRF"),
    s: Session = Depends(db),
) -> BrowserSession:
    item = _get_browser_session(request, s, touch=False)
    if not x_klyrow_csrf or not hmac.compare_digest(item.csrf_hash, sha(x_klyrow_csrf)):
        raise HTTPException(403, "csrf_validation_failed")
    return item


def _session_body(s: Session, item: BrowserSession, csrf: Optional[str] = None) -> dict:
    user = s.get(User, item.user_id)
    memberships = s.scalars(
        select(TenantMember).where(TenantMember.user_id == item.user_id, TenantMember.active == True).order_by(TenantMember.created_at)
    ).all()
    body = {
        "authenticated": True,
        "session_id": item.id,
        "identity_id": item.identity_id,
        "user_id": item.user_id,
        "email": user.email if user else None,
        "tenant_id": item.tenant_id,
        "role": item.role,
        "expires_at": (
            item.expires_at
            if item.expires_at.tzinfo
            else item.expires_at.replace(tzinfo=timezone.utc)
        ).isoformat(),
        "capabilities": sorted(ROLE_PERMISSIONS.get(str(item.role or "").upper(), set())),
        "workspaces": [{"tenant_id": row.tenant_id, "role": row.role} for row in memberships],
    }
    if csrf:
        body["csrf_token"] = csrf
    return body


@router.get("/auth/login")
def login_start(request: Request, return_to: Optional[str] = None, s: Session = Depends(db)):
    return RedirectResponse(_authorization_url(request, s, "login", return_to), status_code=302)


@router.get("/auth/signup")
def signup_start(request: Request, return_to: Optional[str] = None, s: Session = Depends(db)):
    return RedirectResponse(_authorization_url(request, s, "signup", return_to), status_code=302)


@router.get("/auth/google")
def google_start(request: Request, return_to: Optional[str] = None, s: Session = Depends(db)):
    return RedirectResponse(_authorization_url(request, s, "google", return_to), status_code=302)


@router.get("/auth/callback")
def oidc_callback(request: Request, code: str, state: str, s: Session = Depends(db)):
    tx = s.scalar(select(OidcLoginTransaction).where(OidcLoginTransaction.state_hash == sha(state)))
    if not tx or tx.used_at:
        raise HTTPException(410, "oidc_state_invalid_or_used")
    expiry = tx.expires_at if tx.expires_at.tzinfo else tx.expires_at.replace(tzinfo=timezone.utc)
    if expiry <= now():
        raise HTTPException(410, "oidc_state_expired")
    tx.used_at = now()
    s.commit()
    verifier = _decrypt(tx.verifier_ciphertext)
    nonce = _decrypt(tx.nonce_ciphertext)
    tokens = _exchange_code(code, verifier, request)
    claims = _validate_id_token(tokens["id_token"], nonce)
    identity, user, membership = _identity_context(s, claims)
    item, raw, _csrf = _new_session(s, request, identity, user, membership, tokens)
    response = RedirectResponse(_safe_return_to(tx.return_url), status_code=303)
    _set_session_cookie(response, raw)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Klyrow-Session-Id"] = item.id
    return response


@router.get("/auth/session")
def session_status(request: Request, s: Session = Depends(db)):
    try:
        item = _get_browser_session(request, s)
    except HTTPException as exc:
        if exc.status_code == 401:
            return JSONResponse({"authenticated": False}, status_code=200, headers={"Cache-Control": "no-store"})
        raise
    csrf = secrets.token_urlsafe(32)
    item.csrf_hash = sha(csrf)
    s.commit()
    return JSONResponse(_session_body(s, item, csrf), headers={"Cache-Control": "no-store"})


@router.post("/auth/refresh")
def refresh_session(request: Request, current: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    if not current.refresh_ciphertext:
        raise HTTPException(409, "refresh_not_available")
    refresh_token = _decrypt(current.refresh_ciphertext)
    data = {"grant_type": "refresh_token", "client_id": _client_id(), "refresh_token": refresh_token}
    secret = _client_secret()
    if secret:
        data["client_secret"] = secret
    try:
        tokens = _post_oidc_token(data)
    except (httpx.HTTPError, ValueError) as exc:
        current.revoked_at = now()
        s.commit()
        raise HTTPException(401, "refresh_failed") from exc
    if tokens.get("id_token"):
        claims = _validate_id_token(tokens["id_token"])
        identity = s.get(OidcIdentity, current.identity_id)
        if not identity or claims.get("sub") != identity.subject:
            current.revoked_at = now()
            s.commit()
            raise HTTPException(401, "refresh_identity_mismatch")
    else:
        tokens["id_token"] = _decrypt(current.id_token_ciphertext) if current.id_token_ciphertext else ""
    tokens.setdefault("refresh_token", refresh_token)
    identity = s.get(OidcIdentity, current.identity_id)
    user = s.get(User, current.user_id)
    membership = s.scalar(select(TenantMember).where(TenantMember.tenant_id == current.tenant_id, TenantMember.user_id == current.user_id, TenantMember.active == True))
    if not identity or not user or not membership:
        raise HTTPException(403, "workspace_access_denied")
    current.revoked_at = now()
    s.commit()
    item, raw, csrf = _new_session(s, request, identity, user, membership, tokens, rotated_from_id=current.id)
    response = JSONResponse(_session_body(s, item, csrf), headers={"Cache-Control": "no-store"})
    _set_session_cookie(response, raw)
    return response


@router.get("/auth/sessions")
def list_sessions(request: Request, ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    current = _get_browser_session(request, s, touch=False)
    rows = s.scalars(select(BrowserSession).where(BrowserSession.identity_id == ctx["identity_id"]).order_by(BrowserSession.created_at.desc())).all()
    return [
        {
            "id": row.id,
            "current": row.id == current.id,
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
def revoke_session(session_id: str, request: Request, current: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    item = s.get(BrowserSession, session_id)
    if not item or item.identity_id != current.identity_id:
        raise HTTPException(404, "session_not_found")
    item.revoked_at = now()
    s.commit()


@router.post("/auth/logout")
def logout(request: Request, current: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    current.revoked_at = now()
    end_session = _canonical_issuer() + "/protocol/openid-connect/logout?" + urlencode(
        {"client_id": _client_id(), "post_logout_redirect_uri": _public_origin() + "/logged-out"}
    )
    if current.id_token_ciphertext:
        end_session += "&" + urlencode({"id_token_hint": _decrypt(current.id_token_ciphertext)})
    s.commit()
    response = JSONResponse({"logged_out": True, "end_session_url": end_session}, headers={"Cache-Control": "no-store"})
    _clear_session_cookie(response)
    return response


@router.post("/auth/logout-all")
def logout_all(request: Request, current: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    rows = s.scalars(select(BrowserSession).where(BrowserSession.identity_id == current.identity_id, BrowserSession.revoked_at.is_(None))).all()
    for item in rows:
        item.revoked_at = now()
    s.commit()
    response = JSONResponse({"logged_out": True, "revoked_sessions": len(rows)}, headers={"Cache-Control": "no-store"})
    _clear_session_cookie(response)
    return response
