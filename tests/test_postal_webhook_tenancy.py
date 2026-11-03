import json
import time
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app.main import (
    DB,
    Base,
    EmailOutbox,
    Event,
    Message,
    Suppression,
    Tenant,
    engine,
)
from apps.gateway.app import postal_webhook_tenancy
from apps.gateway.app.platform import app
from apps.gateway.app.postal_provisioning import READY, PostalTenantMapping

client = TestClient(app, base_url="https://app.klyrow.test")


def _seed_mapped_outbox():
    Base.metadata.create_all(engine)
    suffix = uuid.uuid4().hex
    tenant_a = f"tenant-postal-a-{suffix}"
    tenant_b = f"tenant-postal-b-{suffix}"
    local_a = f"local-a-{suffix}"
    local_b = f"local-b-{suffix}"
    provider_a = f"postal-a-{suffix}"
    provider_b = f"postal-b-{suffix}"
    with DB() as session:
        session.add_all(
            [
                Tenant(id=tenant_a, name="Postal A", quota=100),
                Tenant(id=tenant_b, name="Postal B", quota=100),
                Message(
                    id=local_a,
                    tenant_id=tenant_a,
                    recipient=f"a-{suffix}@example.com",
                    sender="sender@a.example",
                    subject="A",
                    status="accepted",
                ),
                Message(
                    id=local_b,
                    tenant_id=tenant_b,
                    recipient=f"b-{suffix}@example.com",
                    sender="sender@b.example",
                    subject="B",
                    status="accepted",
                ),
                EmailOutbox(
                    id=f"outbox-a-{suffix}",
                    tenant_id=tenant_a,
                    message_id=local_a,
                    payload="{}",
                    state="delivered",
                    provider_message_id=provider_a,
                ),
                EmailOutbox(
                    id=f"outbox-b-{suffix}",
                    tenant_id=tenant_b,
                    message_id=local_b,
                    payload="{}",
                    state="delivered",
                    provider_message_id=provider_b,
                ),
                PostalTenantMapping(
                    tenant_id=tenant_a,
                    state=READY,
                    provider_mode="Development",
                ),
                PostalTenantMapping(
                    tenant_id=tenant_b,
                    state=READY,
                    provider_mode="Development",
                ),
            ]
        )
        session.commit()
    return {
        "suffix": suffix,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "local_a": local_a,
        "local_b": local_b,
        "provider_a": provider_a,
        "provider_b": provider_b,
    }


def test_tenant_mode_resolves_provider_message_to_exact_mapping(monkeypatch):
    seeded = _seed_mapped_outbox()
    monkeypatch.setenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("KLYROW_POSTAL_TENANT_ID", seeded["tenant_a"])
    with DB() as session:
        resolved = postal_webhook_tenancy.resolve_postal_tenant(
            session,
            provider_message_id=seeded["provider_b"],
            correlation_id=seeded["local_b"],
        )
    assert resolved == seeded["tenant_b"]


def test_tenant_mode_never_falls_back_to_global_tenant(monkeypatch):
    seeded = _seed_mapped_outbox()
    monkeypatch.setenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("KLYROW_POSTAL_TENANT_ID", seeded["tenant_a"])
    with DB() as session:
        try:
            postal_webhook_tenancy.resolve_postal_tenant(
                session,
                provider_message_id="unknown-provider-message",
            )
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 422
            assert getattr(exc, "detail", None) == "postal_tenant_unresolved"
        else:
            raise AssertionError("tenant mode accepted the global tenant fallback")


def test_native_bounce_is_persisted_under_the_resolved_tenant(monkeypatch):
    seeded = _seed_mapped_outbox()
    monkeypatch.setenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "true")
    monkeypatch.setattr(
        postal_webhook_tenancy.main,
        "verify_postal_signature",
        lambda body, signature: None,
    )

    async def delivered(_event_type, _payload):
        return True

    monkeypatch.setattr(postal_webhook_tenancy.main, "emit_middleware", delivered)
    event_id = "postal-event-" + uuid.uuid4().hex
    bounced = f"bounced-{seeded['suffix']}@example.com"
    payload = {
        "uuid": event_id,
        "timestamp": time.time(),
        "event": "MessageBounced",
        "payload": {
            "status": "HardFail",
            "message": {
                "id": "provider-row-" + uuid.uuid4().hex,
                "message_id": seeded["provider_b"],
                "tag": seeded["local_b"],
                "to": bounced,
                "from": "sender@b.example",
            },
        },
    }
    response = client.post(
        "/v1/webhooks/postal-native",
        content=json.dumps(payload),
        headers={"X-Postal-Signature-256": "test-signature"},
    )
    assert response.status_code == 202
    assert response.json()["accepted"] is True

    with DB() as session:
        event = session.get(Event, event_id)
        suppression = session.scalar(select(Suppression).where(Suppression.email == bounced))
        assert event is not None
        assert event.tenant_id == seeded["tenant_b"]
        assert suppression is not None
        assert suppression.tenant_id == seeded["tenant_b"]
