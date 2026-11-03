import os
import secrets
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app.main import DB, Base, Tenant, User, engine, ph, sha
from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff
from apps.gateway.app.auth_bff import SESSION_COOKIE, BrowserSession, OidcLoginTransaction
from apps.gateway.app.tenancy import OidcIdentity, TenantInvitation, TenantMember

ISSUER = "https://auth.codestra.co/realms/codestra"


def _client():
    os.environ["KLYROW_OIDC_ISSUER"] = ISSUER
    os.environ["KLYROW_OIDC_CLIENT_ID"] = "klyrow-portal"
    Base.metadata.create_all(engine)
    return TestClient(app, base_url="https://app.klyrow.test")


def _browser_principal_session(*, disable: str | None = None):
    client = _client()
    suffix = uuid.uuid4().hex
    tenant_id = f"tenant-browser-{suffix}"
    user_id = f"user-browser-{suffix}"
    identity_id = f"identity-browser-{suffix}"
    session_id = f"session-browser-{suffix}"
    raw = "browser_" + uuid.uuid4().hex
    csrf = secrets.token_urlsafe(32)
    with DB() as session:
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            email=f"browser-{suffix}@example.com",
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
        session.add(Tenant(id=tenant_id, name="Browser Tenant", quota=10))
        session.add(user)
        session.add(
            TenantMember(
                id=f"member-{suffix}",
                tenant_id=tenant_id,
                user_id=user_id,
                role="OWNER",
            )
        )
        session.add(identity)
        session.add(
            BrowserSession(
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
                expires_at=auth_bff.now() + timedelta(hours=1),
            )
        )
        session.commit()
        if disable == "user":
            user.enabled = False
        elif disable == "identity":
            identity.enabled = False
        session.commit()
    client.cookies.set(SESSION_COOKIE, raw)
    return client, session_id, csrf


def test_session_reads_keep_one_csrf_token_until_session_rotation():
    client, _session_id, csrf = _browser_principal_session()
    first = client.get("/auth/session")
    second = client.get("/auth/session")
    assert first.status_code == second.status_code == 200
    assert first.json()["csrf_token"] == second.json()["csrf_token"]
    assert first.json()["csrf_token"] == csrf
    logout = client.post("/auth/logout", headers={"X-Klyrow-CSRF": first.json()["csrf_token"]})
    assert logout.status_code == 200


@pytest.mark.parametrize("disabled_record", ["user", "identity"])
def test_disabled_principals_are_revoked_before_reads_or_refresh(disabled_record):
    client, session_id, csrf = _browser_principal_session(disable=disabled_record)
    refresh = client.post("/auth/refresh", headers={"X-Klyrow-CSRF": csrf})
    assert refresh.status_code == 401
    assert refresh.json()["detail"] == "principal_disabled"
    assert client.get("/app/api/context").status_code == 401
    with DB() as session:
        item = session.get(BrowserSession, session_id)
        assert item and item.revoked_at is not None


def test_recovery_uses_supported_keycloak_reset_credentials_flow_without_lookup():
    client = _client()
    response = client.post("/auth/actions/recover", json={"email": "unknown@example.com"})
    assert response.status_code == 202
    location = response.json()["redirect_to"]
    parsed = urlparse(location)
    assert parsed.scheme == "https" and parsed.netloc == "auth.codestra.co"
    assert parsed.path.endswith("/protocol/openid-connect/forgot-credentials")
    assert "unknown@example.com" not in location
    query = parse_qs(parsed.query)
    assert query["code_challenge_method"] == ["S256"]
    with DB() as session:
        transaction = session.scalar(
            select(OidcLoginTransaction).where(
                OidcLoginTransaction.state_hash == sha(query["state"][0])
            )
        )
        assert transaction and transaction.return_url == "/reset-success"


def test_password_and_verification_actions_use_keycloak_aia():
    client = _client()
    password = client.post("/auth/actions/update-password")
    verification = client.post("/auth/actions/verify-email")
    assert password.status_code == verification.status_code == 202
    assert parse_qs(urlparse(password.json()["redirect_to"]).query)["kc_action"] == [
        "UPDATE_PASSWORD"
    ]
    assert parse_qs(urlparse(verification.json()["redirect_to"]).query)["kc_action"] == [
        "VERIFY_EMAIL"
    ]


def test_cancelled_application_action_never_reaches_success_page():
    client = _client()
    start = client.post("/auth/actions/update-password")
    query = parse_qs(urlparse(start.json()["redirect_to"]).query)
    response = client.get(
        f"/auth/callback?state={query['state'][0]}&code=unused&kc_action_status=cancelled",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/service-error"


def test_invitation_is_bound_to_the_oidc_transaction():
    client = _client()
    suffix = uuid.uuid4().hex
    raw = "invite_" + uuid.uuid4().hex
    tenant_id = f"tenant-invite-{suffix}"
    invitation_id = f"invite-{suffix}"
    with DB() as session:
        session.add(Tenant(id=tenant_id, name="Invite Tenant", quota=100))
        session.add(
            TenantInvitation(
                id=invitation_id,
                tenant_id=tenant_id,
                email=f"invitee-{suffix}@example.com",
                role="READ_ONLY",
                token_hash=sha(raw),
                expires_at=auth_bff.now() + timedelta(hours=1),
                created_by="test",
            )
        )
        session.commit()
    valid = client.post("/auth/actions/invitation", json={"token": raw})
    invalid = client.post("/auth/actions/invitation", json={"token": "x" * 32})
    assert valid.status_code == invalid.status_code == 200
    redirect_to = valid.json()["redirect_to"]
    assert redirect_to.startswith(ISSUER + "/protocol/openid-connect/registrations?")
    state = parse_qs(urlparse(redirect_to).query)["state"][0]
    with DB() as session:
        transaction = session.scalar(
            select(OidcLoginTransaction).where(OidcLoginTransaction.state_hash == sha(state))
        )
        assert transaction and transaction.mode == "invite:" + invitation_id
    assert invalid.json() == {"valid": False, "redirect_to": None}


def test_selected_invitation_wins_when_email_has_multiple_valid_invites(monkeypatch):
    client = _client()
    suffix = uuid.uuid4().hex
    email = f"selected-{suffix}@example.com"
    raw_a = "invite_a_" + uuid.uuid4().hex
    raw_b = "invite_b_" + uuid.uuid4().hex
    tenant_a = f"tenant-a-{suffix}"
    tenant_b = f"tenant-b-{suffix}"
    invite_a = f"invite-a-{suffix}"
    invite_b = f"invite-b-{suffix}"
    with DB() as session:
        session.add_all(
            [
                Tenant(id=tenant_a, name="Tenant A", quota=100),
                Tenant(id=tenant_b, name="Tenant B", quota=100),
                TenantInvitation(
                    id=invite_a,
                    tenant_id=tenant_a,
                    email=email,
                    role="READ_ONLY",
                    token_hash=sha(raw_a),
                    expires_at=auth_bff.now() + timedelta(hours=1),
                    created_by="test",
                ),
                TenantInvitation(
                    id=invite_b,
                    tenant_id=tenant_b,
                    email=email,
                    role="ADMIN",
                    token_hash=sha(raw_b),
                    expires_at=auth_bff.now() + timedelta(hours=1),
                    created_by="test",
                ),
            ]
        )
        session.commit()

    start = client.post("/auth/actions/invitation", json={"token": raw_b})
    state = parse_qs(urlparse(start.json()["redirect_to"]).query)["state"][0]
    monkeypatch.setattr(
        auth_bff,
        "_exchange_code",
        lambda code, verifier, request: {
            "id_token": "id-token",
            "refresh_token": "refresh-token",
        },
    )
    monkeypatch.setattr(
        auth_bff,
        "_validate_id_token",
        lambda raw, expected_nonce=None: {
            "iss": ISSUER,
            "sub": f"subject-{suffix}",
            "aud": "klyrow-portal",
            "email": email,
            "email_verified": True,
        },
    )
    callback = client.get(
        f"/auth/callback?state={state}&code=accepted",
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/onboarding"
    with DB() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        memberships = session.scalars(
            select(TenantMember).where(TenantMember.user_id == user.id)
        ).all()
        assert [(row.tenant_id, row.role) for row in memberships] == [(tenant_b, "ADMIN")]
        assert session.get(TenantInvitation, invite_b).accepted_at is not None
        assert session.get(TenantInvitation, invite_a).accepted_at is None


def test_production_invitation_returns_one_time_url_to_authorized_creator():
    client, _session_id, csrf = _browser_principal_session()
    response = client.post(
        "/app/api/team/invitations",
        headers={"X-Klyrow-CSRF": csrf},
        json={
            "email": f"new-invitee-{uuid.uuid4().hex}@example.com",
            "role": "READ_ONLY",
            "expires_hours": 24,
        },
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["delivery_method"] == "ONE_TIME_URL"
    assert body["delivery_state"] == "READY_FOR_SECURE_SHARE"
    token = parse_qs(urlparse(body["invitation_url"]).query)["token"][0]
    with DB() as session:
        invitation = session.get(TenantInvitation, body["id"])
        assert invitation and invitation.token_hash == sha(token)
