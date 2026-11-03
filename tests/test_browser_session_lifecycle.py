import os
import secrets
import uuid
from datetime import UTC, timedelta

from fastapi import Request
from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, Base, Tenant, User, engine, ph, sha
from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff
from apps.gateway.app.auth_bff import SESSION_COOKIE, BrowserSession
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

ISSUER = "https://auth.codestra.co/realms/codestra"


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _client():
    os.environ["KLYROW_OIDC_ISSUER"] = ISSUER
    Base.metadata.create_all(engine)
    return TestClient(app, base_url="https://app.klyrow.test")


def _principal_with_session(*, last_seen_delta=timedelta(), expires_delta=timedelta(hours=1)):
    suffix = uuid.uuid4().hex
    tenant_id = f"tenant-session-{suffix}"
    user_id = f"user-session-{suffix}"
    identity_id = f"identity-session-{suffix}"
    session_id = f"session-{suffix}"
    raw = "browser_" + uuid.uuid4().hex
    csrf = secrets.token_urlsafe(32)
    current = auth_bff.now()
    with DB() as session:
        tenant = Tenant(id=tenant_id, name="Session Tenant", quota=10)
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            email=f"session-{suffix}@example.com",
            password_hash=ph.hash("not-used"),
            role="tenant_admin",
        )
        identity = OidcIdentity(
            id=identity_id,
            issuer=ISSUER,
            subject=f"subject-{suffix}",
            user_id=user_id,
            default_tenant_id=tenant_id,
            identity_type="KLYROW_ONLY",
        )
        membership = TenantMember(
            id=f"member-{suffix}",
            tenant_id=tenant_id,
            user_id=user_id,
            role="OWNER",
        )
        browser = BrowserSession(
            id=session_id,
            token_hash=sha(raw),
            csrf_hash=sha(csrf),
            csrf_ciphertext=auth_bff._encrypt(csrf),
            identity_id=identity_id,
            user_id=user_id,
            tenant_id=tenant_id,
            role="OWNER",
            refresh_ciphertext=auth_bff._encrypt("refresh-token"),
            id_token_ciphertext=auth_bff._encrypt("id-token"),
            created_at=current,
            last_seen_at=current - last_seen_delta,
            expires_at=current + expires_delta,
        )
        session.add_all([tenant, user, identity, membership, browser])
        session.commit()
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "identity_id": identity_id,
        "session_id": session_id,
        "raw": raw,
        "csrf": csrf,
    }


def test_idle_timeout_revokes_inactive_browser_session(monkeypatch):
    monkeypatch.setenv("KLYROW_BROWSER_SESSION_IDLE_SECONDS", "300")
    fixture = _principal_with_session(last_seen_delta=timedelta(seconds=301))
    client = _client()
    client.cookies.set(SESSION_COOKIE, fixture["raw"])

    response = client.get("/app/api/context")
    assert response.status_code == 401
    assert response.json()["detail"] == "session_idle_timeout"
    with DB() as session:
        item = session.get(BrowserSession, fixture["session_id"])
        assert item and item.revoked_at is not None


def test_logout_all_revokes_every_identity_session_for_local_user():
    fixture = _principal_with_session()
    second_identity_id = "identity-second-" + uuid.uuid4().hex
    second_session_id = "session-second-" + uuid.uuid4().hex
    second_csrf = secrets.token_urlsafe(32)
    with DB() as session:
        session.add(
            OidcIdentity(
                id=second_identity_id,
                issuer=ISSUER,
                subject="subject-second-" + uuid.uuid4().hex,
                user_id=fixture["user_id"],
                default_tenant_id=fixture["tenant_id"],
                identity_type="SOCIAL",
            )
        )
        session.add(
            BrowserSession(
                id=second_session_id,
                token_hash=sha("other_" + uuid.uuid4().hex),
                csrf_hash=sha(second_csrf),
                csrf_ciphertext=auth_bff._encrypt(second_csrf),
                identity_id=second_identity_id,
                user_id=fixture["user_id"],
                tenant_id=fixture["tenant_id"],
                role="OWNER",
                expires_at=auth_bff.now() + timedelta(hours=1),
            )
        )
        session.commit()

    client = _client()
    client.cookies.set(SESSION_COOKIE, fixture["raw"])
    response = client.post(
        "/auth/logout-all",
        headers={"X-Klyrow-CSRF": fixture["csrf"]},
    )
    assert response.status_code == 200
    assert response.json()["revoked_sessions"] == 2
    with DB() as session:
        assert session.get(BrowserSession, fixture["session_id"]).revoked_at is not None
        assert session.get(BrowserSession, second_session_id).revoked_at is not None


def test_refresh_rotation_preserves_original_absolute_deadline():
    fixture = _principal_with_session(expires_delta=timedelta(minutes=20))
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("app.klyrow.test", 443),
        "client": ("127.0.0.1", 12345),
        "path": "/auth/refresh",
        "raw_path": b"/auth/refresh",
        "query_string": b"",
        "headers": [(b"user-agent", b"test-browser")],
    }
    request = Request(scope)
    with DB() as session:
        parent = session.get(BrowserSession, fixture["session_id"])
        identity = session.get(OidcIdentity, fixture["identity_id"])
        user = session.get(User, fixture["user_id"])
        membership = (
            session.query(TenantMember)
            .filter_by(tenant_id=fixture["tenant_id"], user_id=fixture["user_id"])
            .one()
        )
        deadline = _utc(parent.expires_at)
        child, _raw, _csrf = auth_bff._new_session(
            session,
            request,
            identity,
            user,
            membership,
            {"id_token": "child-id", "refresh_token": "child-refresh"},
            rotated_from_id=parent.id,
        )
        grandchild, _raw2, _csrf2 = auth_bff._new_session(
            session,
            request,
            identity,
            user,
            membership,
            {"id_token": "grand-id", "refresh_token": "grand-refresh"},
            rotated_from_id=child.id,
        )
        assert _utc(child.expires_at) == deadline
        assert _utc(grandchild.expires_at) == deadline
