import pytest

pytestmark = pytest.mark.usefixtures("canonical_api_owner")

from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, Base, Tenant, User, app, engine, ph, rate_buckets

client = TestClient(app)


def setup_module():
    rate_buckets.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        for tenant_id, role in (
            ("root", "platform_admin"),
            ("ra", "reseller_admin"),
            ("rb", "reseller_admin"),
            ("ca", "tenant_admin"),
            ("cb", "tenant_admin"),
        ):
            s.add(Tenant(id=tenant_id, name=tenant_id, quota=100))
            s.add(
                User(
                    id=tenant_id,
                    tenant_id=tenant_id,
                    email=f"{tenant_id}@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role=role,
                )
            )
        s.commit()


def teardown_module():
    rate_buckets.clear()


def headers(user):
    response = client.post(
        "/v1/auth/login", json={"email": f"{user}@example.com", "password": "long-enough-password"}
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_reseller_customer_is_unique_and_cross_reseller_invisible():
    root = headers("root")
    for tenant in ("ra", "rb"):
        response = client.post(
            "/v1/admin/resellers",
            headers=root,
            json={
                "tenant_id": tenant,
                "name": tenant.upper(),
                "currency": "USD",
                "wholesale_rate": "0.001",
                "credit_limit": "1000",
            },
        )
        assert response.status_code == 201
    created = client.post(
        "/v1/reseller/subaccounts",
        headers=headers("ra"),
        json={"customer_tenant_id": "ca", "retail_rate": "0.002", "quota": 5000},
    )
    assert created.status_code == 201
    assert [
        item["customer_tenant_id"]
        for item in client.get("/v1/reseller/subaccounts", headers=headers("ra")).json()
    ] == ["ca"]
    assert client.get("/v1/reseller/subaccounts", headers=headers("rb")).json() == []
    duplicate = client.post(
        "/v1/reseller/subaccounts",
        headers=headers("rb"),
        json={"customer_tenant_id": "ca", "retail_rate": "0.003", "quota": 5000},
    )
    assert (
        duplicate.status_code == 409 and duplicate.json()["detail"] == "customer_already_assigned"
    )
    assert (
        client.delete(
            f"/v1/reseller/subaccounts/{created.json()['id']}", headers=headers("rb")
        ).status_code
        == 404
    )


def test_customer_tenant_cannot_access_reseller_authority():
    assert client.get("/v1/reseller/subaccounts", headers=headers("ca")).status_code == 404
    assert (
        client.post(
            "/v1/admin/resellers",
            headers=headers("ra"),
            json={
                "tenant_id": "cb",
                "name": "Invalid",
                "currency": "USD",
                "wholesale_rate": "0",
                "credit_limit": "0",
            },
        ).status_code
        == 403
    )
