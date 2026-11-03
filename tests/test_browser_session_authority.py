import os
import uuid
from datetime import timedelta

from fastapi import Request
from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, Base, Tenant, User, engine, ph, sha
from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff
from apps.gateway.app.auth_bff import SESSION_COOKIE, BrowserSession
from apps.gateway.app.browser_auth_actions import stable_csrf_token
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

ISSUER = "https://auth.codestra.co/realms/codestra"


def _seed_session(*, second_identity: bool = False):
    os.environ["KLYROW_OIDC_ISSUER"] = ISSUER
    os.environ["KLYROW_OIDC_CLIENT_ID"] = "klyrow-portal"
    Base.metadata.create_all(engine)
    suffix = uuid.uuid4().hex
    tenant_id = f"tenant-authority-{suffix}"
    user_id = f"user-authority-{suffix}"
    identity_id = f"identity-authority-{suffix}"
    member_id = f"member-authority-{suffix}"
    session_id = f"session-authority-{suffix}"
    raw = "browser_" + uuid.uuid4().hex
    current = auth_bff.now()
    with DB() as session:
        tenant = Tenant(id=tenant_id, name="Authority Tenant", quota=10)
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            email=f"authority-{suffix}@example.com",
            password_hash=ph.hash("not-used"),
            role="tenant_admin",
        )
        membership = TenantMember(
            id=member_id,
            tenant_id=tenant_id,
            user_id=user_id,
            role="OWNER",
        )
        identity = OidcIdentity(
            id=identity_id,
            issuer=ISSUER,
            subject=f"subject-{suffix}",
            user_id=user_id,
            default_tenant_id=tenant_id,
            identity_type="KLYROW_ONLY",
        )
        current_session = BrowserSession(
            id=session_id,
            token_hash=sha(raw),
            csrf_hash=sha(stable_csrf_token(session_id)),
            identity_id=identity_id,
            user_id=user_id,
            tenant_id=tenant_id,
            role="OWNER",
            refresh_ciphertext=auth_bff._encrypt("refresh-token"),
            id_token_ciphertext=auth_bff._encrypt("id-token"),
            created_at=current,
            last_seen_at=current,
            expires_at=current + timedelta(hours=1),
        )
        session.add_all([tenant, user, membership, identity, current_session])
        other_session_id = None
        if second_identity:
            second_identity_id = f"identity-two-{suffix}"
            other_session_id = f"session-two-{suffix}"
            session.add(
                OidcIdentity(
                    id=second_identity_id,
                    issuer=ISSUER,
                    subject=f"subject-two-{suffix}",
                    user_id=user_id,
                    default_tenant_id=tenant_id,
                    identity_type="KLYROW_ONLY",
                )
            )
            session.add(
                BrowserSession(
                    id=other_session_id,
                    token_hash=sha("browser_" + uuid.uuid4().hex),
                    csrf_hash=sha(stable_csrf_token(other_session_id)),
                    identity_id=second_identity_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    role="OWNER",
                    created_at=current,
                    last_seen_at=current,
                    expires_at=current + timedelta(hours=1),
                )
            )
        session.commit()
    client = TestClient(app, base_url="https://app.klyrow.test")
    client.cookies.set(SESSION_COOKIE, raw)
    return {
        "client": client,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "identity_id": identity_id,
        "member_id": member_id,
        "session_id": session_id,
        "other_session_id": other_session_id,
    }


def _assert_revoked(session_id: str):
    with DB() as session:
        assert session.get(BrowserSession, session_id).revoked_at is not None


def test_disabled_user_is_revoked_on_next_browser_request():
    seeded = _seed_session()
    with DB() as session:
        user = session.get(User, seeded["user_id"])
        user.enabled = False
        session.commit()

    response = seeded["client"].get("/auth/session")
    assert response.status_code == 401
    assert response.json()["detail"] == "principal_disabled"
    _assert_revoked(seeded["session_id"])


def test_disabled_identity_cannot_refresh_or_rotate_session():
    seeded = _seed_session()
    with DB() as session:
        identity = session.get(OidcIdentity, seeded["identity_id"])
        identity.enabled = False
        session.commit()

    response = seeded["client"].post(
        "/auth/refresh",
        headers={"X-Klyrow-CSRF": stable_csrf_token(seeded["session_id"])},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "principal_disabled"
    _assert_revoked(seeded["session_id"])


def test_membership_role_changes_are_reloaded_for_existing_sessions():
    seeded = _seed_session()
    with DB() as session:
        member = session.get(TenantMember, seeded["member_id"])
        member.role = "READ_ONLY"
        session.commit()

    response = seeded["client"].get("/auth/session")
    assert response.status_code == 200
    assert response.json()["role"] == "READ_ONLY"
    assert response.json()["capabilities"] == ["analytics.read", "billing.read", "mail.read"]
    assert "mail.send" not in response.json()["capabilities"]


def test_idle_session_is_revoked_before_last_seen_is_touched(monkeypatch):
    seeded = _seed_session()
    monkeypatch.setenv("KLYROW_BROWSER_SESSION_IDLE_SECONDS", "300")
    with DB() as session:
        item = session.get(BrowserSession, seeded["session_id"])
        item.last_seen_at = auth_bff.now() - timedelta(seconds=301)
        session.commit()

    response = seeded["client"].get("/auth/session")
    assert response.status_code == 401
    assert response.json()["detail"] == "session_idle_timeout"
    _assert_revoked(seeded["session_id"])


def test_refresh_rotation_preserves_original_absolute_deadline():
    seeded = _seed_session()
    with DB() as session:
        prior = session.get(BrowserSession, seeded["session_id"])
        original_deadline = auth_bff.now() + timedelta(minutes=7)
        prior.expires_at = original_deadline
        identity = session.get(OidcIdentity, seeded["identity_id"])
        user = session.get(User, seeded["user_id"])
        membership = session.get(TenantMember, seeded["member_id"])
        session.commit()

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/auth/refresh",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("app.klyrow.test", 443),
                "scheme": "https",
                "query_string": b"",
            }
        )
        rotated, _raw, _csrf = auth_bff._new_session(
            session,
            request,
            identity,
            user,
            membership,
            {"refresh_token": "rotated-refresh", "id_token": "rotated-id"},
            rotated_from_id=prior.id,
        )
        deadline = rotated.expires_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=original_deadline.tzinfo)
        assert deadline == original_deadline


def test_logout_all_revokes_sessions_for_every_identity_of_the_user():
    seeded = _seed_session(second_identity=True)
    response = seeded["client"].post(
        "/auth/logout-all",
        headers={"X-Klyrow-CSRF": stable_csrf_token(seeded["session_id"])},
    )
    assert response.status_code == 200
    assert response.json()["revoked_sessions"] == 2
    with DB() as session:
        assert session.get(BrowserSession, seeded["session_id"]).revoked_at is not None
        assert session.get(BrowserSession, seeded["other_session_id"]).revoked_at is not None


def test_session_projection_uses_canonical_capabilities_and_explicit_utc():
    from datetime import datetime

    from apps.gateway.app.tenancy import ROLE_PERMISSIONS

    seeded = _seed_session()
    for role in ["OWNER", "ADMIN", "DEVELOPER", "MARKETING", "BILLING", "SUPPORT", "UNKNOWN"]:
        with DB() as session:
            session.get(TenantMember, seeded["member_id"]).role = role
            session.commit()
        response = seeded["client"].get("/auth/session")
        assert response.status_code == 200
        body = response.json()
        assert body["capabilities"] == sorted(ROLE_PERMISSIONS.get(role, set()))
        assert body["identity_id"] == seeded["identity_id"]
        assert body["tenant_id"] == seeded["tenant_id"]
        assert datetime.fromisoformat(body["expires_at"]).utcoffset() == timedelta(0)
        assert response.headers["cache-control"] == "no-store"
        assert not {"access_token", "refresh_token", "id_token"}.intersection(body)
