import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.gateway.app.main import (
    DB,
    AllowedSender,
    Base,
    Domain,
    Suppression,
    Tenant,
    User,
    app,
    engine,
    ph,
    rate_buckets,
)
from apps.gateway.app.preferences import enforce_suppression

client = TestClient(app)


def setup_module():
    rate_buckets.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        s.add(Tenant(id="a", name="A", quota=100))
        s.add(
            User(
                id="a",
                tenant_id="a",
                email="a@example.com",
                password_hash=ph.hash("long-enough-password"),
                role="tenant_admin",
            )
        )
        s.add(
            Domain(id="domain", tenant_id="a", domain="a.example.com", token="token", verified=True)
        )
        s.add(
            AllowedSender(
                id="sender", tenant_id="a", address="sender@a.example.com", role="support"
            )
        )
        s.commit()


def h():
    response = client.post(
        "/v1/auth/login", json={"email": "a@example.com", "password": "long-enough-password"}
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def mail(recipient, stream="transactional", campaign_id=None):
    return {
        "to": recipient,
        "sender": "sender@a.example.com",
        "subject": "Purpose scoped",
        "html": "<p>synthetic</p>",
        "stream": stream,
        "campaign_id": campaign_id,
    }


def test_one_click_marketing_unsubscribe_is_idempotent_and_does_not_block_transactional():
    created = client.post(
        "/v1/unsubscribe/tokens",
        headers=h(),
        json={"email": "person@example.net", "scope": "TENANT_MARKETING"},
    )
    assert (
        created.status_code == 201
        and created.json()["list_unsubscribe_post"] == "List-Unsubscribe=One-Click"
    )
    token = created.json()["token"]
    assert client.post("/v1/unsubscribe", params={"token": token}).status_code == 200
    assert client.post("/v1/unsubscribe", params={"token": token}).status_code == 200
    marketing = client.post(
        "/v1/email/send",
        headers={**h(), "Idempotency-Key": "marketing-unsubscribed"},
        json=mail("person@example.net", "marketing"),
    )
    assert marketing.status_code == 422 and marketing.json()["detail"] == "recipient_suppressed"
    transactional = client.post(
        "/v1/email/send",
        headers={**h(), "Idempotency-Key": "transactional-allowed"},
        json=mail("person@example.net"),
    )
    assert transactional.status_code == 202


def test_list_unsubscribe_is_campaign_scoped_and_hard_bounce_is_global():
    token = client.post(
        "/v1/unsubscribe/tokens",
        headers=h(),
        json={"email": "list@example.net", "scope": "LIST", "scope_id": "campaign-a"},
    ).json()["token"]
    assert client.post("/v1/unsubscribe", params={"token": token}).json()["scope"] == "LIST"
    with DB() as s:
        with pytest.raises(HTTPException) as denied:
            enforce_suppression(s, "a", "list@example.net", "marketing", "campaign-a")
        assert denied.value.status_code == 422
    with DB() as s:
        s.add(Suppression(id="hard", tenant_id="a", email="hard@example.net", reason="hard_bounce"))
        s.commit()
    assert (
        client.post(
            "/v1/email/send",
            headers={**h(), "Idempotency-Key": "hard-global"},
            json=mail("hard@example.net"),
        ).status_code
        == 422
    )


def test_tampered_unsubscribe_token_is_denied():
    token = client.post(
        "/v1/unsubscribe/tokens",
        headers=h(),
        json={"email": "person@example.net", "scope": "TENANT_MARKETING"},
    ).json()["token"]
    assert client.post("/v1/unsubscribe", params={"token": token[:-2] + "xx"}).status_code == 400
