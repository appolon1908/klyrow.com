import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from apps.gateway.app.main import Base, DB, app, engine
from apps.gateway.app.billing_provider_events import BillingProviderEvent


def _signature(secret: str, body: bytes) -> str:
    timestamp = int(time.time())
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_verified_webhook_is_durable_and_duplicate_safe(monkeypatch, tmp_path):
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    secret = tmp_path / "stripe-webhook"; secret.write_text("whsec_test_fixture")
    api_key = tmp_path / "stripe-api"; api_key.write_text("sk_test_fixture")
    monkeypatch.setenv("KLYROW_BILLING_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_SECRET_FILE", str(api_key))
    monkeypatch.setenv("KLYROW_STRIPE_WEBHOOK_SECRET_FILE", str(secret))
    monkeypatch.setenv("KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED", "true")
    payload = {"id": "evt_durable", "type": "checkout.session.completed", "livemode": False, "data": {"object": {"metadata": {"tenant_id": "tenant", "invoice_id": "invoice", "payment_attempt_id": "attempt"}}}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    client = TestClient(app)
    try:
        headers = {"Stripe-Signature": _signature("whsec_test_fixture", body)}
        first = client.post("/v1/internal/billing/providers/stripe/webhook", content=body, headers=headers)
        second = client.post("/v1/internal/billing/providers/stripe/webhook", content=body, headers=headers)
        assert first.status_code == second.status_code == 202
        assert first.json()["duplicate"] is False and second.json()["duplicate"] is True
        with DB() as session:
            event = session.query(BillingProviderEvent).one()
            assert event.payload_hash == hashlib.sha256(body).hexdigest()
            assert event.processing_state == "RECEIVED"
    finally:
        client.close()


def test_invalid_signature_does_not_persist_event(monkeypatch, tmp_path):
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    secret = tmp_path / "stripe-webhook"; secret.write_text("whsec_test_fixture")
    api_key = tmp_path / "stripe-api"; api_key.write_text("sk_test_fixture")
    monkeypatch.setenv("KLYROW_BILLING_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_SECRET_FILE", str(api_key))
    monkeypatch.setenv("KLYROW_STRIPE_WEBHOOK_SECRET_FILE", str(secret))
    monkeypatch.setenv("KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED", "true")
    client = TestClient(app)
    try:
        response = client.post("/v1/internal/billing/providers/stripe/webhook", content=b'{"id":"evt"}', headers={"Stripe-Signature": "t=1,v1=invalid"})
        assert response.status_code == 400
        with DB() as session: assert session.query(BillingProviderEvent).count() == 0
    finally:
        client.close()