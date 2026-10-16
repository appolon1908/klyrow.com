import os
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app import auth_bff
from apps.gateway.app.auth_bff import BrowserSession, SESSION_COOKIE
from apps.gateway.app.browser_auth_actions import stable_csrf_token
from apps.gateway.app.main import Base, DB, Tenant, User, engine, ph, sha
from apps.gateway.app.platform import app
from apps.gateway.app.support_center import SupportTicket, SupportTicketMessage
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

ISSUER = "https://auth.codestra.co/realms/codestra"
ROOT = Path(__file__).parents[1]


def _principal(label: str):
    os.environ["KLYROW_OIDC_ISSUER"] = ISSUER
    os.environ["KLYROW_OIDC_CLIENT_ID"] = "klyrow-portal"
    Base.metadata.create_all(engine)
    suffix = uuid.uuid4().hex
    tenant_id = f"tenant-support-{label}-{suffix}"
    user_id = f"user-support-{label}-{suffix}"
    identity_id = f"identity-support-{label}-{suffix}"
    session_id = f"session-support-{label}-{suffix}"
    raw = "browser_" + uuid.uuid4().hex
    csrf = stable_csrf_token(session_id)
    with DB() as session:
        session.add(Tenant(id=tenant_id, name=f"Support {label}", quota=100))
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                email=f"support-{label}-{suffix}@example.com",
                password_hash=ph.hash("not-used"),
                role="tenant_user",
            )
        )
        session.add(
            TenantMember(
                id=f"member-{suffix}",
                tenant_id=tenant_id,
                user_id=user_id,
                role="READ_ONLY",
            )
        )
        session.add(
            OidcIdentity(
                id=identity_id,
                issuer=ISSUER,
                subject=f"subject-{suffix}",
                user_id=user_id,
                default_tenant_id=tenant_id,
                identity_type="KLYROW_ONLY",
            )
        )
        session.add(
            BrowserSession(
                id=session_id,
                token_hash=sha(raw),
                csrf_hash=sha(csrf),
                identity_id=identity_id,
                user_id=user_id,
                tenant_id=tenant_id,
                role="READ_ONLY",
                refresh_ciphertext=auth_bff._encrypt("refresh-token"),
                id_token_ciphertext=auth_bff._encrypt("id-token"),
                expires_at=auth_bff.now() + timedelta(hours=1),
            )
        )
        session.commit()
    client = TestClient(app, base_url="https://app.klyrow.test")
    client.cookies.set(SESSION_COOKIE, raw)
    return client, tenant_id, csrf


def test_support_ticket_lifecycle_is_csrf_protected_idempotent_and_tenant_scoped():
    client, tenant_id, csrf = _principal("one")
    other, _other_tenant_id, _other_csrf = _principal("two")
    payload = {
        "subject": "Invoice question",
        "body": "Please explain the latest invoice.",
        "category": "billing",
        "priority": "NORMAL",
    }

    denied = client.post(
        "/app/api/support/tickets",
        headers={"Idempotency-Key": "support-create-1"},
        json=payload,
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "csrf_validation_failed"

    created = client.post(
        "/app/api/support/tickets",
        headers={"X-Klyrow-CSRF": csrf, "Idempotency-Key": "support-create-1"},
        json=payload,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["duplicate"] is False
    assert body["status"] == "OPEN"
    assert body["category"] == "billing"
    assert body["priority"] == "NORMAL"
    assert len(body["messages"]) == 1
    ticket_id = body["id"]

    replay = client.post(
        "/app/api/support/tickets",
        headers={"X-Klyrow-CSRF": csrf, "Idempotency-Key": "support-create-1"},
        json=payload,
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == ticket_id
    assert replay.json()["duplicate"] is True

    conflict = client.post(
        "/app/api/support/tickets",
        headers={"X-Klyrow-CSRF": csrf, "Idempotency-Key": "support-create-1"},
        json={**payload, "body": "Different payload"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_key_payload_mismatch"

    listed = client.get("/app/api/support/tickets")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [ticket_id]

    detail = client.get(f"/app/api/support/tickets/{ticket_id}")
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["body"] == payload["body"]

    cross_tenant = other.get(f"/app/api/support/tickets/{ticket_id}")
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["detail"] == "support_ticket_not_found"

    reply = client.post(
        f"/app/api/support/tickets/{ticket_id}/messages",
        headers={"X-Klyrow-CSRF": csrf, "Idempotency-Key": "support-reply-1"},
        json={"body": "Here is more context."},
    )
    assert reply.status_code == 201
    assert reply.json()["duplicate"] is False
    assert [message["body"] for message in reply.json()["messages"]] == [
        payload["body"],
        "Here is more context.",
    ]

    reply_replay = client.post(
        f"/app/api/support/tickets/{ticket_id}/messages",
        headers={"X-Klyrow-CSRF": csrf, "Idempotency-Key": "support-reply-1"},
        json={"body": "Here is more context."},
    )
    assert reply_replay.status_code == 201
    assert reply_replay.json()["duplicate"] is True
    assert len(reply_replay.json()["messages"]) == 2

    with DB() as session:
        ticket = session.get(SupportTicket, ticket_id)
        assert ticket is not None and ticket.tenant_id == tenant_id
        messages = session.scalars(
            select(SupportTicketMessage).where(SupportTicketMessage.ticket_id == ticket_id)
        ).all()
        assert len(messages) == 2
        assert {message.tenant_id for message in messages} == {tenant_id}


def test_support_contract_has_no_provider_dispatch_boundary():
    source = (ROOT / "apps/gateway/app/support_center.py").read_text(encoding="utf-8")
    migration = (ROOT / "migrations/2026092601_client_portal_support.sql").read_text(encoding="utf-8")
    for forbidden in ("httpx.", "requests.", "postal_url", "telnexa", "smtp", "send_email"):
        assert forbidden not in source.lower()
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "support_tickets" in migration
    assert "support_ticket_messages" in migration


def test_support_browser_routes_are_in_openapi_and_anonymous_requests_hit_auth_boundary():
    schema = app.openapi()
    expected = {
        ("/app/api/support/tickets", "get"),
        ("/app/api/support/tickets", "post"),
        ("/app/api/support/tickets/{ticket_id}", "get"),
        ("/app/api/support/tickets/{ticket_id}/messages", "post"),
    }
    actual = {
        (path, method)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    assert expected <= actual

    anonymous = TestClient(app, base_url="https://app.klyrow.test")
    response = anonymous.get("/app/api/support/tickets")
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication_required"
