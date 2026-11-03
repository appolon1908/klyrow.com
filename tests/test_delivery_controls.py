import pytest

pytestmark = pytest.mark.usefixtures("canonical_api_owner")

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from apps.gateway.app.main import (
    DB,
    AllowedSender,
    Base,
    Domain,
    Tenant,
    User,
    app,
    engine,
    ph,
    rate_buckets,
)

client = TestClient(app)


def setup_module():
    rate_buckets.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        for tenant_id, role in (
            ("root", "platform_admin"),
            ("a", "tenant_admin"),
            ("b", "tenant_admin"),
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
        for tenant_id in ("a", "b"):
            s.add(
                Domain(
                    id="domain-" + tenant_id,
                    tenant_id=tenant_id,
                    domain=f"{tenant_id}.example.com",
                    token="token",
                    verified=True,
                )
            )
            s.add(
                AllowedSender(
                    id="sender-" + tenant_id,
                    tenant_id=tenant_id,
                    address=f"sender@{tenant_id}.example.com",
                    role="support",
                )
            )
        s.commit()


def h(user):
    response = client.post(
        "/v1/auth/login", json={"email": f"{user}@example.com", "password": "long-enough-password"}
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def payload(tenant):
    return {
        "to": "synthetic@example.net",
        "sender": f"sender@{tenant}.example.com",
        "subject": "Synthetic",
        "html": "<p>safe mode</p>",
        "stream": "transactional",
    }


def test_ip_pool_and_warmup_enforce_daily_limit():
    pool = client.post(
        "/v1/admin/ip-pools",
        headers=h("root"),
        json={"name": "shared-primary", "kind": "SHARED", "postal_pool_ref": "postal-pool-primary"},
    )
    assert pool.status_code == 201
    assigned = client.post(
        "/v1/settings/ip-pool-assignment",
        headers=h("a"),
        json={"domain": "a.example.com", "stream": "TRANSACTIONAL", "pool_id": pool.json()["id"]},
    )
    assert assigned.status_code == 201
    warmup = client.post(
        "/v1/settings/warmup",
        headers=h("a"),
        json={
            "domain": "a.example.com",
            "stream": "TRANSACTIONAL",
            "starts_at": datetime.now(UTC).isoformat(),
            "daily_limits": [1, 5, 20],
        },
    )
    assert warmup.status_code == 201
    first = client.post(
        "/v1/email/send", headers={**h("a"), "Idempotency-Key": "warmup-1"}, json=payload("a")
    )
    assert first.status_code == 202
    second = client.post(
        "/v1/email/send", headers={**h("a"), "Idempotency-Key": "warmup-2"}, json=payload("a")
    )
    assert second.status_code == 429 and second.json()["detail"] == "warmup_daily_limit_reached"


def test_scoped_suspension_does_not_affect_other_tenant():
    suspended = client.post(
        "/v1/admin/delivery/suspend",
        headers=h("root"),
        json={
            "tenant_id": "a",
            "resource_type": "DOMAIN",
            "resource_id": "a.example.com",
            "reason": "Synthetic abuse isolation test",
        },
    )
    assert suspended.status_code == 201 and suspended.json()["effective_immediately"] is True
    assert (
        client.post(
            "/v1/email/send",
            headers={**h("a"), "Idempotency-Key": "suspended-a"},
            json=payload("a"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/email/send", headers={**h("b"), "Idempotency-Key": "allowed-b"}, json=payload("b")
        ).status_code
        == 202
    )
    released = client.post(
        f"/v1/admin/delivery/suspensions/{suspended.json()['id']}/release", headers=h("root")
    )
    assert released.status_code == 200 and released.json()["active"] is False


def test_critical_abuse_suspends_only_authoritative_target_tenant():
    response = client.post(
        "/v1/admin/abuse/evaluate",
        headers=h("root"),
        json={
            "tenant_id": "b",
            "bounce_rate": 0.25,
            "complaint_rate": 0.02,
            "invalid_rate": 0.15,
            "volume_ratio": 6,
        },
    )
    assert response.status_code == 201 and response.json()["state"] == "SUSPENDED"
    with DB() as s:
        assert (
            s.get(Tenant, "b").enabled is False
            and s.get(Tenant, "a").enabled is True
            and s.get(Tenant, "root").enabled is True
        )
