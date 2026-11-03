import json

from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, Base, Tenant, User, app, engine, ph
from apps.gateway.app.tenancy import ScopedApiKey, ServiceAccount, SmtpCredential, TenantMember

client = TestClient(app)


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        for tenant_id, role in (
            ("a", "tenant_admin"),
            ("b", "tenant_admin"),
            ("root", "platform_admin"),
        ):
            session.add(Tenant(id=tenant_id, name=tenant_id, quota=10000))
            session.add(
                User(
                    id=tenant_id,
                    tenant_id=tenant_id,
                    email=f"{tenant_id}@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role=role,
                )
            )
        session.commit()


def login(user):
    response = client.post(
        "/v1/auth/login", json={"email": f"{user}@example.com", "password": "long-enough-password"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_multiple_organizations_invitation_acceptance_and_switching():
    a = login("a")
    first = client.post(
        "/v1/organizations", headers=a, json={"name": "Alpha Org", "slug": "alpha-org"}
    )
    second = client.post(
        "/v1/organizations", headers=a, json={"name": "Second Org", "slug": "second-org"}
    )
    assert first.status_code == 201 and second.status_code == 201
    switched = client.post(f"/v1/organizations/{first.json()['tenant_id']}/switch", headers=a)
    assert switched.status_code == 200 and switched.json()["role"] == "OWNER"
    owner = {"Authorization": "Bearer " + switched.json()["access_token"]}
    invite = client.post(
        "/v1/team/invitations", headers=owner, json={"email": "b@example.com", "role": "DEVELOPER"}
    )
    assert invite.status_code == 201 and "token" in invite.json()
    accepted = client.post("/v1/team/invitations/accept", json={"token": invite.json()["token"]})
    assert accepted.status_code == 201 and accepted.json()["role"] == "DEVELOPER"
    b = login("b")
    b_switched = client.post(f"/v1/organizations/{first.json()['tenant_id']}/switch", headers=b)
    assert b_switched.status_code == 200 and b_switched.json()["role"] == "DEVELOPER"
    assert (
        client.post(f"/v1/organizations/{second.json()['tenant_id']}/switch", headers=b).status_code
        == 404
    )


def owner_context():
    base = login("a")
    orgs = client.get("/v1/organizations", headers=base).json()
    tenant = next(item["tenant_id"] for item in orgs if item["slug"] == "alpha-org")
    switched = client.post(f"/v1/organizations/{tenant}/switch", headers=base).json()
    return {"Authorization": "Bearer " + switched["access_token"]}, tenant


def test_service_account_secret_is_displayed_once_rotatable_and_revocable():
    owner, tenant = owner_context()
    created = client.post(
        "/v1/service-accounts",
        headers=owner,
        json={"name": "Worker", "scopes": ["mail.read", "mail.send"]},
    )
    assert created.status_code == 201 and created.json()["client_secret"].startswith("klys_")
    with DB() as session:
        row = session.get(ServiceAccount, created.json()["id"])
        assert created.json()["client_secret"] not in row.secret_hash and json.loads(
            row.scopes_json
        ) == ["mail.read", "mail.send"]
    listed = client.get("/v1/service-accounts", headers=owner).json()
    assert listed[0]["client_id"] == created.json()["client_id"] and "secret_hash" not in listed[0]
    rotated = client.post(f"/v1/service-accounts/{created.json()['id']}/rotate", headers=owner)
    assert (
        rotated.status_code == 200
        and rotated.json()["client_secret"] != created.json()["client_secret"]
    )
    assert (
        client.delete(f"/v1/service-accounts/{created.json()['id']}", headers=owner).status_code
        == 204
    )


def test_scoped_api_and_smtp_credentials_are_hashed_and_tenant_scoped():
    owner, tenant = owner_context()
    key = client.post(
        "/v1/developer/api-keys",
        headers=owner,
        json={
            "name": "Production",
            "scopes": ["mail.send"],
            "environment": "production",
            "ip_allowlist": [],
        },
    )
    smtp = client.post(
        "/v1/developer/smtp-credentials", headers=owner, json={"scopes": ["smtp.send"]}
    )
    assert (
        key.status_code == 201 and smtp.status_code == 201 and smtp.json()["tls_required"] is True
    )
    with DB() as session:
        stored_key = session.get(ScopedApiKey, key.json()["id"])
        stored_smtp = session.get(SmtpCredential, smtp.json()["id"])
        assert (
            stored_key.tenant_id == tenant and key.json()["secret"] not in stored_key.verifier_hash
        )
        assert (
            stored_smtp.tenant_id == tenant
            and smtp.json()["password"] not in stored_smtp.verifier_hash
        )
    keys = client.get("/v1/developer/api-keys", headers=owner).json()
    smtp_rows = client.get("/v1/developer/smtp-credentials", headers=owner).json()
    assert keys[0]["prefix"] == key.json()["prefix"] and "verifier_hash" not in keys[0]
    assert (
        smtp_rows[0]["username"] == smtp.json()["username"] and "verifier_hash" not in smtp_rows[0]
    )
    rotated = client.post(
        f"/v1/developer/smtp-credentials/{smtp.json()['id']}/rotate", headers=owner
    )
    assert rotated.status_code == 200 and rotated.json()["password"] != smtp.json()["password"]
    assert (
        client.delete(f"/v1/developer/api-keys/{key.json()['id']}", headers=owner).status_code
        == 204
    )
    assert (
        client.delete(
            f"/v1/developer/smtp-credentials/{smtp.json()['id']}", headers=owner
        ).status_code
        == 204
    )


def test_owner_cannot_be_removed_and_developer_cannot_manage_team():
    owner, tenant = owner_context()
    with DB() as session:
        session.query(TenantMember).filter_by(tenant_id=tenant, user_id="b").one()
    b = login("b")
    switched = client.post(f"/v1/organizations/{tenant}/switch", headers=b).json()
    developer = {"Authorization": "Bearer " + switched["access_token"]}
    assert (
        client.post(
            "/v1/team/invitations",
            headers=developer,
            json={"email": "new@example.com", "role": "READ_ONLY"},
        ).status_code
        == 403
    )
    assert client.delete("/v1/team/members/a", headers=owner).status_code == 409
