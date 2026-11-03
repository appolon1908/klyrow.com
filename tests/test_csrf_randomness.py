"""M1-07: browser CSRF tokens are random per session, stable across rotation,
and the session id never leaks through a response header."""

import base64
import hashlib
import hmac
import os
import uuid
from datetime import timedelta

from fastapi import Request
from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, SECRET, Base, Tenant, User, engine, ph, sha
from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff
from apps.gateway.app.auth_bff import SESSION_COOKIE, BrowserSession
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

ISSUER = "https://auth.codestra.co/realms/codestra"


def _legacy_derived_token(session_id: str) -> str:
    """The deterministic derivation the finding rejected: HMAC(SECRET, session_id)."""

    digest = hmac.new(
        SECRET.encode(),
        ("klyrow-browser-csrf-v1:" + session_id).encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/refresh",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 12345),
            "server": ("app.klyrow.test", 443),
            "scheme": "https",
            "query_string": b"",
        }
    )


def _seed_principal():
    os.environ["KLYROW_OIDC_ISSUER"] = ISSUER
    os.environ["KLYROW_OIDC_CLIENT_ID"] = "klyrow-portal"
    Base.metadata.create_all(engine)
    suffix = uuid.uuid4().hex
    tenant_id = f"tenant-csrf-{suffix}"
    user_id = f"user-csrf-{suffix}"
    identity_id = f"identity-csrf-{suffix}"
    with DB() as session:
        session.add(Tenant(id=tenant_id, name="CSRF Tenant", quota=10))
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                email=f"csrf-{suffix}@example.com",
                password_hash=ph.hash("not-used"),
                role="tenant_admin",
            )
        )
        session.add(
            TenantMember(id=f"member-{suffix}", tenant_id=tenant_id, user_id=user_id, role="OWNER")
        )
        session.add(
            OidcIdentity(
                id=identity_id,
                issuer=ISSUER,
                subject=f"subject-{suffix}",
                user_id=user_id,
                default_tenant_id=tenant_id,
                identity_type="KLYROW_ONLY",
            )
        )
        session.commit()
    return {"tenant_id": tenant_id, "user_id": user_id, "identity_id": identity_id}


def _new_session(session, seed, rotated_from_id=None):
    identity = session.get(OidcIdentity, seed["identity_id"])
    user = session.get(User, seed["user_id"])
    membership = (
        session.query(TenantMember)
        .filter_by(tenant_id=seed["tenant_id"], user_id=seed["user_id"])
        .one()
    )
    return auth_bff._new_session(
        session,
        _request(),
        identity,
        user,
        membership,
        {"id_token": "id-token", "refresh_token": "refresh-token"},
        rotated_from_id=rotated_from_id,
    )


def _client_for(raw: str) -> TestClient:
    client = TestClient(app, base_url="https://app.klyrow.test")
    client.cookies.set(SESSION_COOKIE, raw)
    return client


def test_two_sessions_for_the_same_user_have_different_csrf_tokens():
    seed = _seed_principal()
    with DB() as session:
        first, _raw1, csrf1 = _new_session(session, seed)
        second, _raw2, csrf2 = _new_session(session, seed)
        assert first.id != second.id
        assert csrf1 != csrf2
        assert first.csrf_hash != second.csrf_hash
        assert len(csrf1) >= 40 and len(csrf2) >= 40


def test_csrf_token_is_not_derived_from_the_session_id():
    seed = _seed_principal()
    with DB() as session:
        item, raw, csrf = _new_session(session, seed)
        assert csrf != _legacy_derived_token(item.id)
        assert item.csrf_hash != sha(_legacy_derived_token(item.id))
    client = _client_for(raw)
    body = client.get("/auth/session").json()
    assert body["authenticated"] is True
    assert body["csrf_token"] == csrf
    assert body["csrf_token"] != _legacy_derived_token(body["session_id"])
    # Knowing the id and SECRET must not unlock the CSRF guard.
    forged = client.post(
        "/auth/logout-all", headers={"X-Klyrow-CSRF": _legacy_derived_token(item.id)}
    )
    assert forged.status_code == 403


def test_rotation_preserves_the_random_csrf_token_and_guard_accepts_it(monkeypatch):
    seed = _seed_principal()
    with DB() as session:
        parent, raw, csrf = _new_session(session, seed)
        parent_id = parent.id
    client = _client_for(raw)
    cached = client.get("/auth/session").json()["csrf_token"]
    assert cached == csrf

    monkeypatch.setattr(
        auth_bff,
        "_post_oidc_token",
        lambda data: {"refresh_token": "rotated-refresh", "expires_in": 300},
    )
    refreshed = client.post("/auth/refresh", headers={"X-Klyrow-CSRF": cached})
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["session_id"] != parent_id
    assert body["csrf_token"] == cached
    with DB() as session:
        child = session.get(BrowserSession, body["session_id"])
        assert child.rotated_from_id == parent_id
        assert child.csrf_hash == sha(cached)
        assert session.get(BrowserSession, parent_id).revoked_at is not None

    # The SPA's cached token keeps working against the rotated session cookie.
    assert client.get("/auth/session").json()["csrf_token"] == cached
    accepted = client.post("/auth/logout-all", headers={"X-Klyrow-CSRF": cached})
    assert accepted.status_code == 200


def test_rotation_in_orm_copies_parent_token_without_extending_deadline():
    seed = _seed_principal()
    with DB() as session:
        parent, _raw, csrf = _new_session(session, seed)
        parent.expires_at = auth_bff.now() + timedelta(minutes=5)
        session.commit()
        child, _raw2, child_csrf = _new_session(session, seed, rotated_from_id=parent.id)
        assert child_csrf == csrf
        assert child.csrf_hash == parent.csrf_hash
        assert child.id != parent.id


def test_auth_responses_never_carry_the_session_id_header(monkeypatch):
    seed = _seed_principal()
    with DB() as session:
        _item, raw, csrf = _new_session(session, seed)
    client = _client_for(raw)
    monkeypatch.setattr(
        auth_bff,
        "_post_oidc_token",
        lambda data: {"refresh_token": "rotated-refresh", "expires_in": 300},
    )
    responses = [
        client.get("/auth/session"),
        client.post("/auth/refresh", headers={"X-Klyrow-CSRF": csrf}),
        client.get("/auth/sessions"),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        assert "x-klyrow-session-id" not in {k.lower() for k in response.headers}

    anonymous = TestClient(app, base_url="https://app.klyrow.test")
    for path in ("/auth/login", "/auth/signup"):
        response = anonymous.get(path, params={"return_to": "/app"}, follow_redirects=False)
        assert "x-klyrow-session-id" not in {k.lower() for k in response.headers}


def test_oidc_callback_redirect_never_carries_the_session_id_header(monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from apps.gateway.app import browser_security_fixes as security
    from apps.gateway.app.auth_bff import OidcLoginTransaction
    from apps.gateway.app.browser_security_fixes import FLOW_COOKIE, flow_binding, oidc_callback

    os.environ["KLYROW_OIDC_ISSUER"] = ISSUER
    os.environ["KLYROW_OIDC_CLIENT_ID"] = "klyrow-portal"
    local = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(local)
    state = "m107-callback-state"
    monkeypatch.setattr(
        auth_bff, "_exchange_code", lambda code, verifier, request: {"id_token": "id-token"}
    )
    monkeypatch.setattr(
        auth_bff,
        "_validate_id_token",
        lambda raw, expected_nonce=None: {"iss": ISSUER, "sub": "s", "aud": "klyrow-portal"},
    )
    monkeypatch.setattr(
        security,
        "_resolve_identity_context_uncommitted",
        lambda session, claims: (
            SimpleNamespace(id="identity-a"),
            SimpleNamespace(id="user-a"),
            SimpleNamespace(tenant_id="tenant-a", role="OWNER"),
        ),
    )
    monkeypatch.setattr(security, "_stage_postal_provisioning", lambda session, tenant_id: None)
    monkeypatch.setattr(
        security,
        "_stage_new_session",
        lambda *args, **kwargs: (SimpleNamespace(id="new-browser-session"), "opaque", "csrf"),
    )
    with sessionmaker(bind=local, expire_on_commit=False)() as session:
        session.add(
            OidcLoginTransaction(
                id="m107-callback",
                state_hash=sha(state),
                verifier_ciphertext=auth_bff._encrypt("verifier"),
                nonce_ciphertext=auth_bff._encrypt("nonce"),
                return_url="/app",
                mode="login",
                expires_at=auth_bff.now() + timedelta(seconds=300),
            )
        )
        session.commit()
        cookie = f"{FLOW_COOKIE}={flow_binding(state)}".encode()
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/auth/callback",
                "raw_path": b"/auth/callback",
                "query_string": b"",
                "headers": [(b"host", b"app.klyrow.test"), (b"cookie", cookie)],
                "client": ("127.0.0.1", 1234),
                "server": ("app.klyrow.test", 443),
                "state": {},
            }
        )
        response = oidc_callback(request, state=state, code="code", session=session)
    assert response.status_code == 303
    assert "x-klyrow-session-id" not in {k.lower() for k in response.headers}
