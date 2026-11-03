import pytest

pytestmark = pytest.mark.usefixtures("canonical_api_owner")

import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("KLYROW_MIDDLEWARE_API_KEY", "integration-result-test-token")

from apps.gateway.app.main import DB, Base, Message, Tenant, User, app, engine, ph, rate_buckets
from apps.gateway.app.messaging import DeliveryJob, WebhookAttempt, WebhookSubscription
from apps.gateway.app.operations import IntegrationOutbox, IntegrationResult, ResultIn, result

client = TestClient(app)
tokens = {}


def setup_module():
    rate_buckets.clear()
    tokens.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        s.add_all(
            [
                Tenant(id="a", name="A", quota=100),
                Tenant(id="b", name="B", quota=100),
                Tenant(id="root", name="Root", quota=100),
                User(
                    id="a",
                    tenant_id="a",
                    email="a@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="tenant_admin",
                ),
                User(
                    id="b",
                    tenant_id="b",
                    email="b@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="tenant_admin",
                ),
                User(
                    id="root",
                    tenant_id="root",
                    email="root@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="platform_admin",
                ),
            ]
        )
        s.commit()


def h(user):
    if user in tokens:
        return tokens[user]
    r = client.post(
        "/v1/auth/login", json={"email": f"{user}@example.com", "password": "long-enough-password"}
    )
    assert r.status_code == 200
    tokens[user] = {"Authorization": "Bearer " + r.json()["access_token"]}
    return tokens[user]


def service_h(tenant):
    return {
        "Authorization": "Bearer " + os.environ["KLYROW_MIDDLEWARE_API_KEY"],
        "X-Klyrow-Tenant-Id": tenant,
    }


def test_support_odoo_and_n8n_use_durable_outbox_not_direct_database():
    support = client.post(
        "/v1/support/tickets",
        headers=h("a"),
        json={
            "category": "deliverability",
            "subject": "DNS review",
            "description": "Please review DNS status",
        },
    )
    assert support.status_code == 201 and support.json()["odoo_sync"] == "QUEUED"
    event = {
        "event_type": "MailDeliveryStatusV1",
        "aggregate_id": "message-1",
        "payload": {"status": "delivered"},
        "idempotency_key": "automation-event-0001",
    }
    first = client.post("/v1/automation/events", headers=h("a"), json=event)
    duplicate = client.post("/v1/automation/events", headers=h("a"), json=event)
    assert (
        first.status_code == 202
        and first.json()["direct_database_write"] is False
        and duplicate.json()["duplicate"] is True
    )
    result_payload = {
        "outbox_id": first.json()["id"],
        "source": "N8N",
        "result_key": "result-event-0001",
        "payload": {"ok": True},
    }
    assert (
        client.post("/v1/integrations/results", headers=h("a"), json=result_payload).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/integrations/results", headers=service_h("b"), json=result_payload
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/integrations/results",
            headers=service_h("a"),
            json={**result_payload, "source": "ODOO"},
        ).status_code
        == 403
    )
    result = client.post("/v1/integrations/results", headers=service_h("a"), json=result_payload)
    assert result.status_code == 202
    replay = client.post("/v1/integrations/results", headers=service_h("a"), json=result_payload)
    assert replay.status_code == 202 and replay.json()["duplicate"] is True
    assert (
        client.post(
            "/v1/integrations/results",
            headers=service_h("a"),
            json={**result_payload, "payload": {"ok": False}},
        ).status_code
        == 409
    )
    billing = client.post(
        "/v1/billing/odoo-sync",
        headers=h("a"),
        json={**event, "idempotency_key": "billing-sync-0001"},
    )
    assert billing.json()["direct_odoo_database_write"] is False


def test_export_closure_and_immediate_kill_switch_preserve_data():
    export = client.post(
        "/v1/exports", headers=h("a"), json={"scopes": ["account", "contacts", "billing", "audit"]}
    )
    assert export.status_code == 202 and export.json()["asynchronous"] is True
    disabled = client.put(
        "/v1/settings/send-gate",
        headers=h("a"),
        json={"enabled": False, "reason": "Emergency owner stop"},
    )
    assert (
        disabled.json()["effective_immediately"] is True
        and disabled.json()["sending_enabled"] is False
    )
    blocked = client.post(
        "/v1/messages",
        headers={**h("a"), "Idempotency-Key": "kill-switch-send-0001"},
        json={
            "to": "sink@example.com",
            "sender": "sender@example.com",
            "subject": "blocked",
            "html": "blocked",
        },
    )
    assert blocked.status_code == 403 and blocked.json()["detail"] == "tenant_send_gate_disabled"
    closure = client.post(
        "/v1/account/closure",
        headers=h("a"),
        json={"grace_days": 30, "retention_policy": "STANDARD"},
    )
    assert closure.status_code == 202 and closure.json()["sending_enabled"] is False
    confirmed = client.post(
        f"/v1/account/closure/{closure.json()['id']}/confirm",
        headers=h("a"),
        json={"confirmation": closure.json()["confirmation"]},
    )
    assert confirmed.json()["state"] == "CONFIRMED" and confirmed.json()["data_erased"] is False
    assert (
        client.post(
            f"/v1/account/closure/{closure.json()['id']}/confirm",
            headers=h("b"),
            json={"confirmation": closure.json()["confirmation"]},
        ).status_code
        == 404
    )


def test_audited_dead_letter_and_webhook_recovery_are_admin_only():
    with DB() as s:
        s.add_all(
            [
                DeliveryJob(
                    id="dead-job",
                    tenant_id="a",
                    message_id="dead-message",
                    state="DEAD_LETTER",
                    attempts=5,
                ),
                WebhookSubscription(
                    id="hook",
                    tenant_id="a",
                    url="https://example.com",
                    events_json='["message.delivered"]',
                    secret_hash="hash",
                    encrypted_secret_ref="secret://hook",
                ),
                WebhookAttempt(
                    id="attempt",
                    tenant_id="a",
                    subscription_id="hook",
                    event_id="event",
                    event_type="message.delivered",
                    state="DEAD_LETTER",
                    attempts=5,
                ),
            ]
        )
        s.commit()
    assert (
        client.post(
            "/v1/admin/operations/delivery-jobs/dead-job/recover",
            headers=h("a"),
            json={"reason": "manual retry"},
        ).status_code
        == 403
    )
    recovered = client.post(
        "/v1/admin/operations/delivery-jobs/dead-job/recover",
        headers=h("root"),
        json={"reason": "provider recovered"},
    )
    assert recovered.json()["state"] == "RETRY"
    replayed = client.post(
        "/v1/admin/operations/webhooks/attempt/replay",
        headers=h("root"),
        json={"reason": "endpoint recovered"},
    )
    assert replayed.json()["state"] == "PENDING"


def test_reconciliation_detects_missing_outbox_without_silent_repair():
    with DB() as s:
        s.add(
            Message(
                id="orphan-queued",
                tenant_id="a",
                recipient="r@example.com",
                sender="s@example.com",
                subject="x",
                status="queued",
            )
        )
        s.commit()
    report = client.post("/v1/admin/reconciliation", headers=h("root"))
    assert (
        report.status_code == 201
        and report.json()["state"] == "DRIFT"
        and report.json()["drift_count"] >= 1
        and report.json()["auto_corrected"] is False
    )


def test_n8n_and_odoo_outages_preserve_events_for_audited_recovery():
    n8n = client.post(
        "/v1/automation/events",
        headers=h("a"),
        json={
            "event_type": "MailDeliveryStatusV1",
            "aggregate_id": "outage-message",
            "payload": {"status": "delivered"},
            "idempotency_key": "n8n-outage-event-0001",
        },
    ).json()
    odoo = client.post(
        "/v1/billing/odoo-sync",
        headers=h("a"),
        json={
            "event_type": "InvoiceCreatedV1",
            "aggregate_id": "outage-invoice",
            "payload": {"status": "open"},
            "idempotency_key": "odoo-outage-event-0001",
        },
    ).json()
    for item_id, target in ((n8n["id"], "N8N"), (odoo["id"], "ODOO")):
        assert (
            client.post(
                f"/v1/admin/operations/integrations/{item_id}/fail",
                headers=h("a"),
                json={"reason": "downstream unavailable"},
            ).status_code
            == 403
        )
        failed = client.post(
            f"/v1/admin/operations/integrations/{item_id}/fail",
            headers=h("root"),
            json={"reason": "downstream unavailable"},
        )
        assert (
            failed.status_code == 200
            and failed.json()["state"] == "RETRY"
            and failed.json()["target"] == target
        )
        with DB() as s:
            row = s.get(IntegrationOutbox, item_id)
            assert (
                row.payload_json
                and row.idempotency_key
                and row.last_error == "downstream unavailable"
            )
        recovered = client.post(
            f"/v1/admin/operations/integrations/{item_id}/recover",
            headers=h("root"),
            json={"reason": "downstream restored"},
        )
        assert recovered.status_code == 200 and recovered.json()["state"] == "PENDING"


def test_terminal_integration_cannot_be_requeued_by_admin_failure():
    queued = client.post(
        "/v1/automation/events",
        headers=h("a"),
        json={
            "event_type": "TerminalStateV1",
            "aggregate_id": "terminal-item",
            "payload": {"ok": True},
            "idempotency_key": "terminal-state-event-0001",
        },
    ).json()
    with DB() as s:
        row = s.get(IntegrationOutbox, queued["id"])
        row.state = "CANCELLED"
        s.commit()
    failed = client.post(
        f"/v1/admin/operations/integrations/{queued['id']}/fail",
        headers=h("root"),
        json={"reason": "late failure"},
    )
    assert failed.status_code == 404
    with DB() as s:
        assert s.get(IntegrationOutbox, queued["id"]).state == "CANCELLED"


def test_in_flight_integration_cannot_be_requeued_by_admin_failure():
    queued = client.post(
        "/v1/automation/events",
        headers=h("a"),
        json={
            "event_type": "InFlightStateV1",
            "aggregate_id": "in-flight-item",
            "payload": {"ok": True},
            "idempotency_key": "in-flight-state-event-0001",
        },
    ).json()
    with DB() as s:
        row = s.get(IntegrationOutbox, queued["id"])
        row.state = "PROCESSING"
        s.commit()
    failed = client.post(
        f"/v1/admin/operations/integrations/{queued['id']}/fail",
        headers=h("root"),
        json={"reason": "provider timeout not yet confirmed"},
    )
    assert failed.status_code == 404
    with DB() as s:
        assert s.get(IntegrationOutbox, queued["id"]).state == "PROCESSING"


class ConcurrentResultSession:
    def __init__(self, prior):
        self.outbox = IntegrationOutbox(
            id="concurrent-outbox",
            tenant_id="a",
            target="N8N",
            event_type="ConcurrentV1",
            aggregate_id="aggregate",
            payload_json="{}",
            idempotency_key="concurrent-command",
            state="PENDING",
        )
        self.prior = prior
        self.scalar_calls = 0
        self.rolled_back = False

    def scalar(self, statement):
        del statement
        self.scalar_calls += 1
        return (
            self.outbox
            if self.scalar_calls == 1
            else None
            if self.scalar_calls == 2
            else self.prior
        )

    def add(self, item):
        del item

    def commit(self):
        raise IntegrityError(
            "INSERT integration_results", {}, RuntimeError("concurrent unique key")
        )

    def rollback(self):
        self.rolled_back = True


def test_concurrent_scoped_result_key_returns_durable_winner():
    payload_json = '{"ok":true}'
    prior = IntegrationResult(
        id="winner",
        tenant_id="a",
        outbox_id="concurrent-outbox",
        source="N8N",
        result_key="concurrent-result-key",
        payload_json=payload_json,
    )
    session = ConcurrentResultSession(prior)
    response = result(
        ResultIn(
            outbox_id="concurrent-outbox",
            source="N8N",
            result_key="concurrent-result-key",
            payload={"ok": True},
        ),
        {"tenant": "a", "sub": "middleware-service"},
        session,
    )
    assert response == {"id": "winner", "duplicate": True} and session.rolled_back


def test_concurrent_scoped_result_key_semantic_conflict_is_409():
    prior = IntegrationResult(
        id="winner",
        tenant_id="a",
        outbox_id="different-outbox",
        source="N8N",
        result_key="concurrent-result-key",
        payload_json='{"ok":true}',
    )
    session = ConcurrentResultSession(prior)
    with pytest.raises(HTTPException) as caught:
        result(
            ResultIn(
                outbox_id="concurrent-outbox",
                source="N8N",
                result_key="concurrent-result-key",
                payload={"ok": True},
            ),
            {"tenant": "a", "sub": "middleware-service"},
            session,
        )
    assert (
        getattr(caught.value, "status_code", None) == 409
        and getattr(caught.value, "detail", None) == "integration_result_idempotency_conflict"
        and session.rolled_back
    )
