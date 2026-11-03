import pytest

pytestmark = pytest.mark.usefixtures("canonical_api_owner")

import os

os.environ.update(
    KLYROW_DATABASE_URL="sqlite:///./test-agent-mailboxes.db",
    KLYROW_SESSION_SECRET="test-session-secret-at-least-32-bytes",
    KLYROW_SAFE_MODE="true",
    KLYROW_ENV="test",
)
from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, Base, Domain, Tenant, User, app, engine, ph
from apps.gateway.app.agent_mailboxes import AgentMailbox, normalize_first_name

client = TestClient(app)


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        s.add_all([Tenant(id="tenant-a", name="A"), Tenant(id="tenant-b", name="B")])
        s.add_all(
            [
                User(
                    id="root",
                    tenant_id="tenant-a",
                    email="root@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="platform_admin",
                ),
                User(
                    id="agent-a",
                    tenant_id="tenant-a",
                    email="agent-a@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="codestra-email-agent",
                ),
                User(
                    id="admin-b",
                    tenant_id="tenant-b",
                    email="admin-b@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="tenant_admin",
                ),
            ]
        )
        s.commit()


def hdr(email):
    token = client.post(
        "/v1/auth/login", json={"email": email, "password": "long-enough-password"}
    ).json()["access_token"]
    return {"Authorization": "Bearer " + token}


def event(
    agent="agent-1",
    first="María José",
    event_id="event-1",
    campaign="campaign-a",
    keycloak="agent-a",
):
    return {
        "event_id": event_id,
        "agent_id": agent,
        "employee_id": "employee-1",
        "odoo_user_id": "odoo-1",
        "vicidial_user_id": "vic-1",
        "keycloak_user_id": keycloak,
        "campaign_id": campaign,
        "campaign_name": "Campaign A",
        "first_name": first,
        "last_name": "Example",
        "display_name": first + " Example",
        "supervisor_id": "supervisor-1",
        "active": True,
        "correlation_id": "correlation-1",
    }


def add_mapping():
    return client.post(
        "/v1/campaign-email-domains",
        headers=hdr("root@example.com"),
        json={
            "campaign_id": "campaign-a",
            "campaign_name": "Campaign A",
            "primary_domain": "codestra.co",
            "sender_domain_verified": True,
            "inbound_domain_verified": True,
            "sending_enabled": True,
            "receiving_enabled": True,
            "human_mailbox_enabled": True,
            "domain_classification": "HUMAN_CAMPAIGN",
            "status": "active",
        },
    )


def test_first_name_normalization():
    assert normalize_first_name("José") == "jose"
    assert normalize_first_name("María José") == "mariajose"
    assert normalize_first_name("Jean-Pierre") == "jeanpierre"
    assert normalize_first_name("D’Angelo") == "dangelo"


def test_missing_mapping_blocks_without_guessing():
    r = client.post("/v1/agent-mailboxes/provision", headers=hdr("root@example.com"), json=event())
    assert r.status_code == 409 and r.json()["detail"] == "BLOCKED_DOMAIN_MAPPING_REQUIRED"


def test_provision_replay_conflict_activation_and_sender_isolation():
    assert add_mapping().status_code == 201
    admin = hdr("root@example.com")
    first = client.post("/v1/agent-mailboxes/provision", headers=admin, json=event())
    assert first.status_code == 202
    assert (
        first.json()["primary_email"] == "mariajose@codestra.co"
        and first.json()["mailbox_status"] == "VALIDATION_PENDING"
    )
    replay = client.post("/v1/agent-mailboxes/provision", headers=admin, json=event())
    assert (
        replay.json()["already_existed"] is True
        and replay.json()["mailbox_id"] == first.json()["mailbox_id"]
    )
    collision = client.post(
        "/v1/agent-mailboxes/provision",
        headers=admin,
        json=event(agent="agent-2", event_id="event-2"),
    )
    assert collision.status_code == 409
    assert collision.json()["detail"] == {
        "code": "EMAIL_ADDRESS_CONFLICT",
        "original_request": "mariajose@codestra.co",
        "suggested_address": "mariajoseexample@codestra.co",
    }
    resolved = client.post(
        "/v1/agent-mailboxes/provision",
        headers=admin,
        json={
            **event(agent="agent-2", event_id="event-2"),
            "approved_local_part": "mariajoseexample",
        },
    )
    assert (
        resolved.status_code == 202
        and resolved.json()["primary_email"] == "mariajoseexample@codestra.co"
    )
    next_collision = client.post(
        "/v1/agent-mailboxes/provision",
        headers=admin,
        json=event(agent="agent-3", event_id="event-3"),
    )
    assert next_collision.json()["detail"]["suggested_address"] == "mariajoseexample2@codestra.co"
    active = client.post(
        f"/v1/agent-mailboxes/{first.json()['mailbox_id']}/validate",
        headers=admin,
        json={"outbound_validated": True, "inbound_validated": True},
    )
    assert active.json()["mailbox_status"] == "ACTIVE"
    with DB() as s:
        s.add(
            Domain(
                id="domain-a",
                tenant_id="tenant-a",
                domain="codestra.co",
                token="verified",
                verified=True,
            )
        )
        s.commit()
    agent = hdr("agent-a@example.com")
    payload = {
        "to": "synthetic@example.net",
        "sender": "mariajose@codestra.co",
        "subject": "Synthetic",
        "html": "<p>non-delivery</p>",
        "campaign_id": "campaign-a",
    }
    assert (
        client.post(
            "/v1/email/send", headers={**agent, "Idempotency-Key": "agent-send-1"}, json=payload
        ).status_code
        == 202
    )
    assert (
        client.post(
            "/v1/email/send",
            headers={**agent, "Idempotency-Key": "agent-send-no-campaign"},
            json={key: value for key, value in payload.items() if key != "campaign_id"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/email/send",
            headers={**agent, "Idempotency-Key": "agent-send-2"},
            json={**payload, "sender": "support@codestra.co"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/agent-mailboxes/{first.json()['mailbox_id']}/suspend", headers=admin
        ).json()["mailbox_status"]
        == "SUSPENDED"
    )
    assert (
        client.post(
            "/v1/email/send", headers={**agent, "Idempotency-Key": "agent-send-3"}, json=payload
        ).status_code
        == 403
    )


def test_tenant_isolation_hides_other_mailboxes():
    assert client.get("/v1/agent-mailboxes", headers=hdr("admin-b@example.com")).json() == {
        "items": []
    }


def test_standalone_mailbox_does_not_require_odoo_account():
    admin = hdr("root@example.com")
    payload = event(
        agent="standalone-1",
        first="Francois",
        event_id="standalone-event",
        keycloak="standalone-sub",
    )
    payload["last_name"] = "Person"
    payload["odoo_user_id"] = None
    payload["employee_id"] = None
    made = client.post("/v1/agent-mailboxes/provision", headers=admin, json=payload)
    assert made.status_code == 202 and made.json()["primary_email"] == "francois@codestra.co"
    with DB() as s:
        assert s.get(AgentMailbox, made.json()["mailbox_id"]).odoo_user_id is None


def test_system_domain_cannot_enable_human_mailboxes():
    r = client.post(
        "/v1/campaign-email-domains",
        headers=hdr("root@example.com"),
        json={
            "campaign_id": "system",
            "campaign_name": "System",
            "primary_domain": "klyrow.com",
            "human_mailbox_enabled": True,
            "domain_classification": "SYSTEM_OR_SERVICE",
        },
    )
    assert (
        r.status_code == 422
        and r.json()["detail"] == "human_mailbox_requires_human_campaign_domain"
    )
