import os

os.environ.update(
    KLYROW_DATABASE_URL="sqlite:///./test-communications-provider.db",
    KLYROW_SESSION_SECRET="test-secret-provider-32-characters-minimum",
    KLYROW_SAFE_MODE="true",
    KLYROW_ENV="test",
    KLYROW_SANDBOX_DOMAIN="klyrow-sink.test",
)

from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, AllowedSender, Base, Domain, Tenant, app, auth, engine
from apps.gateway.app.provider import (
    DkimKey,
    ProviderDomain,
    ProviderEvent,
    ProviderMessage,
    SenderIdentity,
)

client = TestClient(app)
identity = {
    "sub": "middleware-service",
    "tenant": "tenant-comms",
    "role": "platform_admin",
    "service": True,
    "permissions": ["klyrow.send", "klyrow.read"],
}


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        session.add(Tenant(id="tenant-comms", name="Communications", quota=100))
        session.add(
            Domain(
                id="legacy-comms-domain",
                tenant_id="tenant-comms",
                domain="communications.example.com",
                token="verified",
                verified=True,
            )
        )
        session.add(
            AllowedSender(
                id="allowed-comms-support",
                tenant_id="tenant-comms",
                address="support@communications.example.com",
                role="support",
                enabled=True,
            )
        )
        provider_domain = ProviderDomain(
            id="provider-comms-domain",
            tenant_id="tenant-comms",
            domain="communications.example.com",
            status="VERIFIED",
            ownership_token="token",
            sending_enabled=False,
        )
        session.add(provider_domain)
        session.add(
            SenderIdentity(
                id="sender-comms",
                tenant_id="tenant-comms",
                domain_id="provider-comms-domain",
                email="support@communications.example.com",
                stream="TRANSACTIONAL",
                status="ACTIVE",
            )
        )
        session.add(
            DkimKey(
                id="dkim-comms",
                tenant_id="tenant-comms",
                domain_id="provider-comms-domain",
                selector="k1",
                version=1,
                public_value="v=DKIM1; p=test",
                private_secret_ref="file:/safe",
                status="ACTIVE",
            )
        )
        session.commit()


def setup_function():
    app.dependency_overrides[auth] = lambda: identity


def teardown_function():
    app.dependency_overrides.pop(auth, None)


def headers(key="canonical-provider-key", correlation="canonical-provider-correlation"):
    return {"Idempotency-Key": key, "X-Correlation-Id": correlation}


def payload(**changes):
    value = {
        "channel": "email",
        "from": "support@communications.example.com",
        "to": ["capture@klyrow-sink.test"],
        "content": {"subject": "Step 3", "text": "canonical safe-mode message"},
        "metadata": {"stream": "TRANSACTIONAL", "consent": "granted"},
    }
    value.update(changes)
    return value


def test_canonical_provider_send_is_service_only_idempotent_and_safe_mode():
    first = client.post(
        "/v1/internal/email/communications/messages", headers=headers(), json=payload()
    )
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["channel"] == "email"
    assert body["provider"] == "klyrow"
    assert body["status"] == "queued"
    assert body["metadata"]["sandbox"] is True

    replay = client.post(
        "/v1/internal/email/communications/messages", headers=headers(), json=payload()
    )
    assert replay.status_code == 202
    assert replay.json()["messageId"] == body["messageId"]

    conflict = client.post(
        "/v1/internal/email/communications/messages",
        headers=headers(),
        json=payload(content={"subject": "Changed", "text": "changed"}),
    )
    assert conflict.status_code == 409

    identity["service"] = False
    denied = client.post(
        "/v1/internal/email/communications/messages",
        headers=headers("service-denied", "service-denied-correlation"),
        json=payload(),
    )
    assert denied.status_code == 403
    identity["service"] = True


def test_canonical_provider_readbacks_domain_health_and_reputation():
    created = client.post(
        "/v1/internal/email/communications/messages",
        headers=headers("readback-key", "readback-correlation"),
        json=payload(),
    ).json()
    message = client.get(f"/v1/internal/email/communications/messages/{created['messageId']}")
    assert message.status_code == 200
    assert message.json()["correlationId"] == "readback-correlation"

    with DB() as session:
        session.add(
            ProviderEvent(
                id="event-1",
                tenant_id="tenant-comms",
                message_id=created["messageId"],
                kind="message.delivered",
                payload_json='{"provider_event":"delivered"}',
            )
        )
        session.get(ProviderMessage, created["messageId"]).status = "DELIVERED"
        session.commit()

    events = client.get(f"/v1/internal/email/communications/messages/{created['messageId']}/events")
    assert events.status_code == 200
    assert events.json()["items"][0]["status"] == "delivered"

    domains = client.get("/v1/internal/email/communications/domains")
    assert domains.status_code == 200
    assert domains.json()["items"][0]["checks"]["dkim"] == "valid"

    health = client.get("/v1/internal/email/communications/provider-health")
    assert health.status_code == 200
    assert health.json()["status"] == "disabled"

    reputation = client.get("/v1/internal/email/communications/reputation")
    assert reputation.status_code == 200
    assert reputation.json()["providers"][0]["provider"] == "klyrow"
