import os
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, Base, Tenant, User, app, engine, ph
from apps.gateway.app import main
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

client = TestClient(app)
issuer = "https://auth.codestra.co/realms/codestra"


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        s.add_all(
            [
                Tenant(id="a", name="A", quota=100),
                Tenant(id="b", name="B", quota=100),
                User(
                    id="a",
                    tenant_id="a",
                    email="a@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="tenant_admin",
                ),
                TenantMember(id="member-a", tenant_id="a", user_id="a", role="OWNER"),
                OidcIdentity(
                    id="oidc-a",
                    issuer=issuer,
                    subject="keycloak-subject-a",
                    user_id="a",
                    default_tenant_id="a",
                    identity_type="KLYROW_ONLY",
                ),
            ]
        )
        s.commit()


def token(private, **changes):
    payload = {
        "iss": issuer,
        "sub": "keycloak-subject-a",
        "aud": "klyrow-api",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "scope": "openid profile email",
    }
    payload.update(changes)
    return jwt.encode(payload, private, algorithm="RS256", headers={"kid": "test"})


def test_oidc_config_requires_pkce_and_disables_local_password():
    body = client.get("/v1/auth/oidc/config").json()
    assert (
        body["issuer"] == issuer
        and body["code_challenge_method"] == "S256"
        and body["local_password_login"] is False
    )


def test_legacy_portal_no_longer_exchanges_or_stores_oidc_tokens():
    source = (Path(__file__).parents[1] / "apps/gateway/app/portal.js").read_text()
    assert 'location.assign("/login")' in source
    assert "location.origin" not in source
    assert "sessionStorage" not in source
    assert "token_endpoint" not in source
    assert 'grant_type:"authorization_code"' not in source


def test_production_local_login_is_disabled():
    old = os.environ.get("KLYROW_ENV")
    os.environ["KLYROW_ENV"] = "production"
    try:
        response = client.post(
            "/v1/auth/login", json={"email": "a@example.com", "password": "long-enough-password"}
        )
        assert response.status_code == 410
    finally:
        if old is None:
            os.environ.pop("KLYROW_ENV", None)
        else:
            os.environ["KLYROW_ENV"] = old


def test_canonical_oidc_signature_audience_identity_and_membership():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    main._jwks_clients[issuer] = SimpleNamespace(
        get_signing_key_from_jwt=lambda raw: SimpleNamespace(key=private.public_key())
    )
    valid = {"Authorization": "Bearer " + token(private)}
    response = client.get("/v1/me", headers=valid)
    assert response.status_code == 200, response.text
    assert response.json()["tenant"] == "a" and response.json()["identity_type"] == "KLYROW_ONLY"
    assert client.get("/v1/me", headers={**valid, "X-Klyrow-Tenant-ID": "b"}).status_code == 403
    assert (
        client.get(
            "/v1/me", headers={"Authorization": "Bearer " + token(private, aud="wrong")}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/v1/me",
            headers={
                "Authorization": "Bearer "
                + token(private, iss="https://auth.codestra.agency/realms/codestra")
            },
        ).status_code
        == 401
    )
