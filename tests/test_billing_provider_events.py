import hashlib
import hmac
import json
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from apps.gateway.app.main import Base, DB, app, engine
from apps.gateway.app.billing_provider_events import BillingProviderEvent, claim_provider_events, now, recover_expired_provider_events


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


def test_production_webhook_requires_live_charging(monkeypatch, tmp_path):
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    secret = tmp_path / "stripe-webhook"; secret.write_text("whsec_test_fixture")
    api_key = tmp_path / "stripe-api"; api_key.write_text("sk_live_fixture")
    monkeypatch.setenv("KLYROW_BILLING_ENABLED", "true")
    monkeypatch.setenv("KLYROW_LIVE_CHARGING_ENABLED", "false")
    monkeypatch.setenv("KLYROW_STRIPE_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_ENVIRONMENT", "production")
    monkeypatch.setenv("KLYROW_STRIPE_PRODUCTION_APPROVED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_SECRET_FILE", str(api_key))
    monkeypatch.setenv("KLYROW_STRIPE_WEBHOOK_SECRET_FILE", str(secret))
    monkeypatch.setenv("KLYROW_STRIPE_CURRENCY_ALLOWLIST", "USD")
    monkeypatch.setenv("KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED", "true")
    client = TestClient(app)
    try:
        response = client.post("/v1/internal/billing/providers/stripe/webhook", content=b'{"id":"evt","type":"checkout.session.completed"}', headers={"Stripe-Signature": "t=1,v1=invalid"})
        assert response.status_code == 503
        assert response.json()["detail"] == "live_charging_disabled"
    finally:
        client.close()


def test_expired_processing_provider_events_are_recovered():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    with DB() as session:
        retry = BillingProviderEvent(id="retry", provider="stripe", provider_event_id="evt_retry", event_type="payment_intent.succeeded", payload_json="{}", payload_hash="hash", processing_state="PROCESSING", attempt_count=1, claimed_at=now()-timedelta(seconds=61), claimed_by="dead-worker")
        dead = BillingProviderEvent(id="dead", provider="stripe", provider_event_id="evt_dead", event_type="payment_intent.succeeded", payload_json="{}", payload_hash="hash", processing_state="PROCESSING", attempt_count=8, claimed_at=now()-timedelta(seconds=61), claimed_by="dead-worker")
        session.add_all([retry, dead]); session.commit()
        assert recover_expired_provider_events(session) == 2
        assert retry.processing_state == "RETRY" and retry.claimed_by is None
        assert dead.processing_state == "DEAD_LETTER" and dead.last_error_code == "provider_event_claim_expired"


def test_expired_provider_event_is_claimable_again():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    with DB() as session:
        event = BillingProviderEvent(id="claim", provider="stripe", provider_event_id="evt_claim", event_type="payment_intent.succeeded", payload_json="{}", payload_hash="hash", processing_state="PROCESSING", attempt_count=1, claimed_at=now()-timedelta(seconds=61), claimed_by="dead-worker")
        session.add(event); session.commit()
        claimed = claim_provider_events(session, "new-worker")
        assert claimed == [event]
        assert event.processing_state == "PROCESSING" and event.claimed_by == "new-worker"