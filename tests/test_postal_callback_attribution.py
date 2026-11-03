import base64
import json
import time
import uuid
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app.main import (
    DB,
    Audit,
    Base,
    EmailOutbox,
    Event,
    Message,
    PostalEvent,
    Suppression,
    Tenant,
    engine,
)
from apps.gateway.app.platform import app
from apps.gateway.app import postal_callback_attribution as callback
from apps.gateway.app.tenant_postal_delivery import attributed_postal_payload

client = TestClient(app, base_url="https://api.klyrow.test")


def _configure_signing_key(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_path = tmp_path / "postal-webhook-public.pem"
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setenv("KLYROW_POSTAL_WEBHOOK_PUBLIC_KEY", str(public_path))
    return private_key


def _signed_post(private_key, payload):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = base64.b64encode(
        private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    return client.post(
        "/v1/webhooks/postal-native",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Postal-Signature-256": signature,
        },
    )


def _postal_payload(*, event_id, provider_message_id, tag, recipient):
    return {
        "event": "MessageDeliveryFailed",
        "timestamp": time.time(),
        "uuid": event_id,
        "payload": {
            "status": "HardFail",
            "message": {
                "id": 12345,
                "message_id": provider_message_id,
                "tag": tag,
                "to": recipient,
                "from": "sender@klyrow.com",
                "subject": "Delivery test",
                "token": "postal-token",
            },
        },
    }


def test_only_one_postal_native_callback_route_is_registered():
    routes = [
        route
        for route in app.routes
        if getattr(route, "path", "") == "/v1/webhooks/postal-native"
        and "POST" in getattr(route, "methods", set())
    ]
    assert len(routes) == 1
    assert routes[0].endpoint.__module__.endswith("postal_callback_attribution")


def test_tenant_delivery_payload_contains_opaque_local_callback_tag():
    payload = attributed_postal_payload(
        '{"to":["person@example.com"],"subject":"Hello"}',
        "local-message-123",
    )
    assert payload["tag"] == "local-message-123"
    assert "tenant_id" not in payload


def test_signed_callback_resolves_tenant_from_local_outbound_mapping(tmp_path, monkeypatch):
    Base.metadata.create_all(engine)
    private_key = _configure_signing_key(tmp_path, monkeypatch)
    monkeypatch.setenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "true")
    suffix = uuid.uuid4().hex
    wrong_tenant = f"tenant-global-{suffix}"
    correct_tenant = f"tenant-local-{suffix}"
    local_message_id = f"message-{suffix}"
    provider_message_id = f"postal-{suffix}"
    event_id = f"event-{suffix}"
    recipient = f"bounce-{suffix}@example.com"
    monkeypatch.setenv("KLYROW_POSTAL_TENANT_ID", wrong_tenant)
    monkeypatch.setattr(callback.core, "emit_middleware", AsyncMock(return_value=True))

    with DB() as session:
        session.add_all(
            [
                Tenant(id=wrong_tenant, name="Wrong Global Tenant", quota=10),
                Tenant(id=correct_tenant, name="Correct Local Tenant", quota=10),
                Message(
                    id=local_message_id,
                    tenant_id=correct_tenant,
                    recipient=recipient,
                    sender="sender@klyrow.com",
                    subject="Delivery test",
                    status="accepted",
                ),
                EmailOutbox(
                    id=f"outbox-{suffix}",
                    tenant_id=correct_tenant,
                    message_id=local_message_id,
                    operation_id=f"operation-{suffix}",
                    correlation_id=f"correlation-{suffix}",
                    payload="{}",
                    state="delivered",
                    provider_message_id=provider_message_id,
                ),
            ]
        )
        session.commit()

    response = _signed_post(
        private_key,
        _postal_payload(
            event_id=event_id,
            provider_message_id=provider_message_id,
            tag=local_message_id,
            recipient=recipient,
        ),
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"accepted": True}

    with DB() as session:
        postal_event = session.get(PostalEvent, event_id)
        assert postal_event and postal_event.tenant_id == correct_tenant
        assert postal_event.message_id == local_message_id
        normalized = json.loads(postal_event.payload)
        assert normalized["tenant_id"] == correct_tenant
        assert normalized["operation_id"] == f"operation-{suffix}"
        assert normalized["correlation_id"] == f"correlation-{suffix}"
        assert len(normalized["payload_hash"]) == 64
        assert normalized["metadata"]["tenant_attribution"] in {
            "outbox_tag",
            "message_tag",
        }
        event = session.get(Event, event_id)
        assert event and event.tenant_id == correct_tenant
        assert event.message_id == local_message_id
        suppression = session.scalar(
            select(Suppression).where(
                Suppression.tenant_id == correct_tenant,
                Suppression.email == recipient,
            )
        )
        assert suppression and suppression.reason == "bounced"
        assert not session.scalar(
            select(Suppression).where(
                Suppression.tenant_id == wrong_tenant,
                Suppression.email == recipient,
            )
        )
        audit = session.scalar(
            select(Audit).where(
                Audit.tenant_id == correct_tenant,
                Audit.action == "email.status.bounced",
            )
        )
        assert audit is not None


def test_tenant_callback_fails_closed_when_no_local_attribution_exists(tmp_path, monkeypatch):
    Base.metadata.create_all(engine)
    private_key = _configure_signing_key(tmp_path, monkeypatch)
    monkeypatch.setenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "true")
    suffix = uuid.uuid4().hex
    global_tenant = f"tenant-global-unmapped-{suffix}"
    event_id = f"event-unmapped-{suffix}"
    monkeypatch.setenv("KLYROW_POSTAL_TENANT_ID", global_tenant)
    with DB() as session:
        session.add(Tenant(id=global_tenant, name="Global Tenant", quota=10))
        session.commit()

    response = _signed_post(
        private_key,
        _postal_payload(
            event_id=event_id,
            provider_message_id=f"unknown-provider-{suffix}",
            tag=f"unknown-tag-{suffix}",
            recipient=f"unknown-{suffix}@example.com",
        ),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "postal_tenant_attribution_unavailable"
    with DB() as session:
        assert session.get(PostalEvent, event_id) is None
        assert session.get(Event, event_id) is None
