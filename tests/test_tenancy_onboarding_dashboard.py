import os
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app.main import DB, Base, Message, Tenant, User, engine, sha
from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff
from apps.gateway.app.auth_bff import BrowserSession
from apps.gateway.app.saas import Onboarding
from apps.gateway.app.tenancy import OidcIdentity, Organization, TenantMember
from apps.gateway.app.tenancy_onboarding import IdentityProfileRecord, resolve_identity_context

issuer = "https://auth.codestra.co/realms/codestra"
client = TestClient(app, base_url="https://app.klyrow.test")


def setup_module():
    os.environ["KLYROW_OIDC_ISSUER"] = issuer
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_first_login_creates_canonical_identity_workspace_and_profile():
    with DB() as s:
        identity, user, membership = resolve_identity_context(
            s,
            {
                "sub": "new-subject",
                "email": "new@example.com",
                "email_verified": True,
                "name": "New Company",
            },
        )
        assert identity.subject == "new-subject" and identity.issuer == issuer
        assert membership.role == "OWNER" and identity.default_tenant_id == membership.tenant_id
        assert (
            s.scalar(select(Organization).where(Organization.tenant_id == membership.tenant_id))
            is not None
        )
        assert s.get(Onboarding, membership.tenant_id) is not None
        profile = s.get(IdentityProfileRecord, identity.id)
        assert profile.email == "new@example.com" and profile.email_verified is True


def test_email_cannot_silently_link_a_different_oidc_subject():
    with DB() as s:
        user = s.scalar(select(User).where(User.email == "new@example.com"))
        assert user is not None
        try:
            resolve_identity_context(
                s, {"sub": "attacker-subject", "email": "new@example.com", "email_verified": True}
            )
        except Exception as exc:
            assert (
                getattr(exc, "status_code", None) == 409
                and getattr(exc, "detail", "") == "identity_link_review_required"
            )
        else:
            raise AssertionError("email-based identity takeover was allowed")


def _browser_login_cookie():
    raw = "browser-session-fixture"
    with DB() as s:
        identity = s.scalar(select(OidcIdentity).where(OidcIdentity.subject == "new-subject"))
        member = s.scalar(select(TenantMember).where(TenantMember.user_id == identity.user_id))
        s.add(
            BrowserSession(
                id="browser-a",
                token_hash=sha(raw),
                csrf_hash=sha("csrf-a"),
                identity_id=identity.id,
                user_id=identity.user_id,
                tenant_id=member.tenant_id,
                role=member.role,
                created_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            )
        )
        s.commit()
        return raw, member.tenant_id


def test_dashboard_is_tenant_scoped_and_mutations_require_csrf():
    raw, tenant_id = _browser_login_cookie()
    client.cookies.set(auth_bff.SESSION_COOKIE, raw, path="/")
    with DB() as s:
        other = Tenant(id="other", name="Other", quota=10)
        s.add(other)
        s.add(
            Message(
                id="other-message",
                tenant_id="other",
                recipient="x@example.com",
                sender="a@example.com",
                subject="secret-other-tenant",
                status="queued",
            )
        )
        s.add(
            Message(
                id="own-message",
                tenant_id=tenant_id,
                recipient="owner@example.com",
                sender="sender@example.com",
                subject="own",
                status="queued",
            )
        )
        s.commit()
    response = client.get("/app/api/dashboard")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metrics"]["messages_total"] == 1 and body["recent_messages"][0]["subject"] == "own"
    assert (
        client.patch(
            "/app/api/onboarding", json={"step": 2, "checklist": {"profile": True}}
        ).status_code
        == 403
    )
    session = client.get("/auth/session").json()
    updated = client.patch(
        "/app/api/onboarding",
        headers={"X-Klyrow-CSRF": session["csrf_token"]},
        json={"step": 2, "use_case": "transactional", "checklist": {"profile": True}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["checklist"]["profile"] is True


def test_owner_cannot_access_platform_admin_dashboard():
    response = client.get("/app/api/admin/dashboard")
    assert response.status_code == 403, response.text


def test_product_routes_use_built_spa_contract():
    assert any(getattr(route, "path", None) == "/app" for route in app.routes)
    assert any(getattr(route, "path", None) == "/onboarding" for route in app.routes)
