import os
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, Base, Tenant, User, engine, ph
from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff
from apps.gateway.app.auth_bff import BrowserSession, OidcLoginTransaction
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

issuer = "https://auth.codestra.co/realms/codestra"
client = TestClient(app, base_url="https://app.klyrow.test")


def setup_module():
    os.environ["KLYROW_OIDC_ISSUER"] = issuer
    os.environ["KLYROW_OIDC_CLIENT_ID"] = "klyrow-portal"
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        s.add(Tenant(id="tenant-a", name="Tenant A", quota=100))
        s.add(
            User(
                id="user-a",
                tenant_id="tenant-a",
                email="owner@example.com",
                password_hash=ph.hash("not-used-by-oidc"),
                role="tenant_admin",
            )
        )
        s.add(TenantMember(id="member-a", tenant_id="tenant-a", user_id="user-a", role="OWNER"))
        s.add(
            OidcIdentity(
                id="identity-a",
                issuer=issuer,
                subject="subject-a",
                user_id="user-a",
                default_tenant_id="tenant-a",
                identity_type="KLYROW_ONLY",
            )
        )
        s.commit()


def test_login_start_persists_pkce_and_never_redirects_off_canonical_issuer():
    response = client.get(
        "/auth/login?return_to=https://evil.example/steal", follow_redirects=False
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(issuer + "/protocol/openid-connect/auth?")
    query = parse_qs(urlparse(location).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["client_id"] == ["klyrow-portal"]
    with DB() as s:
        tx = s.scalar(
            __import__("sqlalchemy")
            .select(OidcLoginTransaction)
            .where(OidcLoginTransaction.state_hash == auth_bff.sha(query["state"][0]))
        )
        assert tx is not None and tx.return_url == "/app"
        assert query["nonce"][0] not in tx.nonce_ciphertext
    assert "code_verifier" not in location


def test_production_login_uses_exact_callback_and_rejects_noncanonical_hosts(monkeypatch):
    monkeypatch.setenv("KLYROW_ENV", "production")
    monkeypatch.setenv("KLYROW_PUBLIC_URL", "https://app.klyrow.com")
    monkeypatch.setenv("KLYROW_OIDC_REDIRECT_URI", "https://app.klyrow.com/auth/callback")
    production_client = TestClient(app, base_url="https://app.klyrow.com")

    response = production_client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://app.klyrow.com/auth/callback"]

    for host in ("api.klyrow.com", "track.klyrow.com", "track.codestra.co"):
        rejected = production_client.get(
            "/auth/login", headers={"Host": host}, follow_redirects=False
        )
        assert rejected.status_code == 421, (host, rejected.status_code, rejected.text)
        assert rejected.json()["detail"] == "canonical_app_host_required"


def test_production_rejects_noncanonical_callback_configuration(monkeypatch):
    monkeypatch.setenv("KLYROW_ENV", "production")
    monkeypatch.setenv("KLYROW_PUBLIC_URL", "https://app.klyrow.com")
    monkeypatch.setenv("KLYROW_OIDC_REDIRECT_URI", "https://track.codestra.co/auth/callback")
    production_client = TestClient(app, base_url="https://app.klyrow.com")

    response = production_client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 503
    assert response.json()["detail"] == "oidc_redirect_uri_misconfigured"


def test_callback_sets_opaque_cookie_session_and_state_is_single_use(monkeypatch):
    start = client.get("/auth/login?return_to=/app", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    monkeypatch.setattr(
        auth_bff,
        "_exchange_code",
        lambda code, verifier, request: {
            "id_token": "id-token-secret",
            "refresh_token": "refresh-token-secret",
        },
    )
    monkeypatch.setattr(
        auth_bff,
        "_validate_id_token",
        lambda raw, expected_nonce=None: {
            "iss": issuer,
            "sub": "subject-a",
            "aud": "klyrow-portal",
        },
    )
    callback = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert callback.status_code == 303 and callback.headers["location"] == "/app"
    cookie = callback.headers["set-cookie"]
    assert (
        "__Host-klyrow_session=" in cookie
        and "HttpOnly" in cookie
        and "Secure" in cookie
        and "Path=/" in cookie
    )
    assert "refresh-token-secret" not in cookie and "id-token-secret" not in cookie
    replay = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert replay.status_code == 410
    with DB() as s:
        row = s.scalar(__import__("sqlalchemy").select(BrowserSession))
        assert (
            row and row.refresh_ciphertext and "refresh-token-secret" not in row.refresh_ciphertext
        )


def test_session_status_csrf_and_logout(monkeypatch):
    status = client.get("/auth/session")
    assert status.status_code == 200 and status.json()["authenticated"] is True
    body = status.json()
    assert "csrf_token" in body and "refresh_token" not in body and "id_token" not in body
    assert client.post("/auth/logout").status_code == 403
    response = client.post("/auth/logout", headers={"X-Klyrow-CSRF": body["csrf_token"]})
    assert response.status_code == 200 and response.json()["logged_out"] is True
    logout_query = parse_qs(urlparse(response.json()["end_session_url"]).query)
    assert logout_query["post_logout_redirect_uri"] == ["https://app.klyrow.com/logged-out"]
    assert client.get("/auth/session").json() == {"authenticated": False}


def test_browser_bundle_does_not_store_tokens():
    source = (__import__("pathlib").Path(__file__).parents[1] / "apps/web/src/App.vue").read_text()
    assert "sessionStorage" not in source and "localStorage" not in source
    assert "refresh_token" not in source and "access_token" not in source
