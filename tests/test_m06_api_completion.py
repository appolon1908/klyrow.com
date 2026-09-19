"""Mission M06: Klyrow Public API completion gaps.

Covers the routes closed against the Klyrow Public API Contract:
message events/batch aliases, template versions, campaign test send,
webhook delivery introspection/replay/rotate-secret, and the customer-facing
suppression check. Mirrors the existing test_messaging.py / test_api.py style.
"""
import socket
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import HTTPException
import pytest

from apps.gateway.app.main import AllowedSender, Base, DB, Domain, Tenant, User, app, engine, ph, rate_buckets

client = TestClient(app)
tokens = {}


@pytest.mark.parametrize("role", ["BILLING", "SUPPORT", "READ_ONLY"])
@pytest.mark.parametrize("operation", ["replay", "rotate"])
def test_webhook_mutations_require_management_before_database_access(role, operation):
    from apps.gateway.app.messaging import webhook_delivery_replay, webhook_rotate_secret
    from apps.gateway.app.capabilities import mutation_permission

    context = {"tenant": "a", "role": role}
    # No session is supplied: authorization must reject before any data access.
    with pytest.raises(HTTPException) as error:
        if operation == "replay":
            webhook_delivery_replay("webhook", "delivery", context, None)
        else:
            webhook_rotate_secret("webhook", context, None)
    assert error.value.status_code == 403
    assert mutation_permission("POST", "/v1/webhooks/webhook/rotate-secret") == "webhook.manage"


def setup_module():
    rate_buckets.clear()
    tokens.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        for tid in ("a", "b"):
            s.add(Tenant(id=tid, name=tid, quota=10000))
            s.add(User(id=tid, tenant_id=tid, email=f"{tid}@example.com",
                       password_hash=ph.hash("long-enough-password"), role="tenant_admin"))
            s.add(Domain(id=tid, tenant_id=tid, domain=f"{tid}.example.com", token=tid, verified=True))
            s.add(AllowedSender(id=tid, tenant_id=tid, address=f"sender@{tid}.example.com", role="support"))
        s.commit()


def headers(tid):
    if tid in tokens:
        return tokens[tid]
    response = client.post("/v1/auth/login", json={"email": f"{tid}@example.com", "password": "long-enough-password"})
    assert response.status_code == 200
    tokens[tid] = {"Authorization": "Bearer " + response.json()["access_token"]}
    return tokens[tid]


def test_message_events_alias_matches_canonical_route():
    h = {**headers("a"), "Idempotency-Key": "m06-events-alias"}
    payload = {"to": "events-alias@example.net", "sender": "sender@a.example.com", "subject": "hi", "html": "<p>hi</p>"}
    sent = client.post("/v1/messages", headers=h, json=payload)
    assert sent.status_code == 202
    mid = sent.json()["id"]
    canonical = client.get(f"/v1/email/{mid}/events", headers=headers("a"))
    alias = client.get(f"/v1/messages/{mid}/events", headers=headers("a"))
    assert alias.status_code == 200
    assert alias.json() == canonical.json()


def test_messages_batch_alias_delegates_to_bulk_send():
    payload = {
        "messages": [
            {"to": "batch-1@example.net", "sender": "sender@a.example.com", "subject": "b1", "html": "<p>b1</p>"},
            {"to": "batch-2@example.net", "sender": "sender@a.example.com", "subject": "b2", "html": "<p>b2</p>"},
        ]
    }
    response = client.post("/v1/messages/batch", headers={**headers("a"), "Idempotency-Key": "m06-batch-1"}, json=payload)
    assert response.status_code == 202
    assert response.json()["accepted"] == 2
    # Without an Idempotency-Key it fails exactly like /v1/email/bulk does.
    denied = client.post("/v1/messages/batch", headers=headers("a"), json=payload)
    assert denied.status_code == 400 and denied.json()["detail"] == "idempotency_key_required"


def test_campaign_test_send_is_truthful_fixture_and_idempotent():
    h = headers("a")
    created = client.post("/v1/campaigns", headers={**h, "Idempotency-Key": "m06-campaign-1"},
                           json={"name": "M06 campaign", "subject": "Subject"})
    assert created.status_code == 201
    cid = created.json()["id"]
    first = client.post(f"/v1/campaigns/{cid}/test", headers={**h, "Idempotency-Key": "m06-campaign-test-1"})
    assert first.status_code == 200
    assert first.json()["provider_submission"] is False and first.json()["internal_sink"] is True
    replay = client.post(f"/v1/campaigns/{cid}/test", headers={**h, "Idempotency-Key": "m06-campaign-test-1"})
    assert replay.status_code == 200 and replay.json() == first.json()
    assert client.post(f"/v1/campaigns/{cid}/test", headers={**headers("b"), "Idempotency-Key": "m06-campaign-test-2"}).status_code == 404
    cancelled = client.post(f"/v1/campaigns/{cid}/cancel", headers={**h, "Idempotency-Key": "m06-campaign-cancel-1"})
    assert cancelled.status_code == 200
    terminal = client.post(f"/v1/campaigns/{cid}/test", headers={**h, "Idempotency-Key": "m06-campaign-test-3"})
    assert terminal.status_code == 409 and terminal.json()["detail"] == "campaign_terminal"


def test_template_versions_list_and_detail():
    h = headers("a")
    created = client.post("/v1/templates", headers=h, json={
        "slug": "m06-welcome", "name": "Welcome", "subject": "Hello {{name}}",
        "html_body": "<h1>Hello {{name}}</h1>", "text_body": "Hello {{name}}", "variables": ["name"],
    })
    assert created.status_code == 201
    tid = created.json()["id"]
    updated = client.put(f"/v1/templates/{tid}", headers=h, json={
        "subject": "Welcome {{name}}", "html_body": "<p>Welcome {{name}}</p>",
        "text_body": "Welcome {{name}}", "variables": ["name"],
    })
    assert updated.json()["version"] == 2
    versions = client.get(f"/v1/templates/{tid}/versions", headers=h)
    assert versions.status_code == 200
    assert [item["version"] for item in versions.json()["items"]] == [2, 1]
    version_id = versions.json()["items"][-1]["id"]
    first_version = client.get(f"/v1/templates/{tid}/versions/{version_id}", headers=h)
    assert first_version.status_code == 200 and first_version.json()["subject"] == "Hello {{name}}"
    missing = client.get(f"/v1/templates/{tid}/versions/99", headers=h)
    assert missing.status_code == 404
    assert client.get(f"/v1/templates/{tid}/versions", headers=headers("b")).status_code == 404


def test_suppression_check_reports_reason_and_absence():
    h = headers("a")
    created = client.post("/v1/suppressions", headers=h, json={"email": "blocked@example.net", "reason": "hard_bounce"})
    assert created.status_code == 201
    hit = client.get("/v1/suppressions/check", headers=h, params={"email": "Blocked@Example.NET"})
    assert hit.status_code == 200
    assert hit.json() == {"email": "blocked@example.net", "suppressed": True, "reason": "hard_bounce"}
    miss = client.get("/v1/suppressions/check", headers=h, params={"email": "clean@example.net"})
    assert miss.json() == {"email": "clean@example.net", "suppressed": False, "reason": None}
    assert client.get("/v1/suppressions/check", headers=headers("b"), params={"email": "blocked@example.net"}).json()["suppressed"] is False


def test_webhook_deliveries_replay_and_rotate_secret():
    h = headers("a")
    # Keep the real URL/IP policy while making this lifecycle test independent of DNS.
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
    with patch("apps.gateway.app.main.socket.getaddrinfo", return_value=addresses):
        webhook = client.post("/v1/webhook-subscriptions", headers=h, json={"url": "https://example.com/m06-events", "events": ["message.delivered"]})
    assert webhook.status_code == 201, webhook.text
    wid = webhook.json()["id"]
    original_secret = webhook.json()["secret"]
    event = {"event_id": "m06-delivery-0001", "event_type": "message.delivered", "payload": {"message_id": "m"}}
    attempt = client.post(f"/v1/webhook-subscriptions/{wid}/test", headers=h, json=event)
    assert attempt.status_code == 202
    delivery_id = attempt.json()["id"]

    listed = client.get(f"/v1/webhooks/{wid}/deliveries", headers=h)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [delivery_id]
    assert client.get(f"/v1/webhooks/{wid}/deliveries", headers=headers("b")).status_code == 404

    detail = client.get(f"/v1/webhooks/{wid}/deliveries/{delivery_id}", headers=h)
    assert detail.status_code == 200 and detail.json()["id"] == delivery_id
    assert client.get(f"/v1/webhooks/{wid}/deliveries/missing", headers=h).status_code == 404

    replay = client.post(f"/v1/webhooks/{wid}/deliveries/{delivery_id}/replay", headers=h)
    assert replay.status_code == 202
    assert replay.json() == {"id": delivery_id, "state": "PENDING", "provider_submission": False}

    rotated = client.post(f"/v1/webhooks/{wid}/rotate-secret", headers=h)
    assert rotated.status_code == 200
    assert "secret" in rotated.json() and rotated.json()["secret"] != original_secret
    assert rotated.json()["rotated_at"] is not None
    subscriptions = client.get("/v1/webhook-subscriptions", headers=h).json()
    match = next(item for item in subscriptions if item["id"] == wid)
    assert match["rotated_at"] is not None
