"""Exercise the public ASGI routes against the real governed send transaction."""

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("KLYROW_DATABASE_URL", "sqlite:///./test-middleware-email.db")
os.environ.setdefault("KLYROW_SESSION_SECRET", "test-middleware-email-session-secret-only-32")
os.environ.setdefault("KLYROW_SAFE_MODE", "true")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.gateway.app import main as core
from apps.gateway.app.middleware_email import EmailCommandBinding
from apps.gateway.app.platform import app


@pytest.fixture
def gateway(monkeypatch):
    postgres = os.getenv("KLYROW_CONTRACT_POSTGRES_URL")
    admin = None
    if postgres:
        schema = "contract_" + uuid.uuid4().hex
        admin = create_engine(postgres)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(postgres, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    core.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    context = {"tenant": "tenant-a", "sub": "middleware-a", "service": True,
               "role": "service", "permissions": ["klyrow.send", "klyrow.read"]}
    with sessions() as session:
        for tenant in ("tenant-a", "tenant-b"):
            session.add(core.Tenant(id=tenant, name=tenant, quota=100))
        session.flush()
        for tenant in ("tenant-a", "tenant-b"):
            session.add(core.Domain(id=tenant, tenant_id=tenant, domain="example.com", token=tenant, verified=True))
            session.add(core.AllowedSender(id=tenant, tenant_id=tenant, address="sender@example.com", role="support"))
        session.commit()
    def db():
        with sessions() as session:
            yield session
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[core.auth] = lambda: dict(context)
    app.dependency_overrides[core.db] = db
    monkeypatch.setattr(core, "SAFE_MODE", True)
    monkeypatch.setenv("KLYROW_ENV", "test")
    client = TestClient(app, raise_server_exceptions=True)
    yield client, sessions, context
    client.close()
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    engine.dispose()
    if admin is not None:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def document(**changes):
    return {"message_id": "command-00000001", "tenant_id": "tenant-a",
            "sender": "sender@example.com", "recipients": ["recipient@example.net"],
            "subject": "Contract", "text": "Synthetic", "html": None,
            "stream": "transactional", "metadata": {}, **changes}


HEADERS = {"Idempotency-Key": "email-command-key-0001", "X-Correlation-ID": "email-correlation-0001"}


def production_authorization():
    now = datetime.now(timezone.utc)
    return {
        "schemaVersion": "1.0",
        "tenantId": "tenant-a",
        "policyVersion": 2,
        "mode": "TRANSACTIONAL_PRODUCTION",
        "authorizationState": "ACTIVE",
        "killSwitchOpen": True,
        "changeId": "CHG-EMAIL-001",
        "category": "transactional",
        "validFrom": (now - timedelta(minutes=5)).isoformat(),
        "validUntil": (now + timedelta(hours=1)).isoformat(),
        "provider": "klyrow-postal",
        "environment": "production",
        "approvedReleaseSha": "a" * 40,
        "authorizationTimestamp": (now - timedelta(minutes=10)).isoformat(),
        "activationTimestamp": (now - timedelta(minutes=5)).isoformat(),
        "commandBinding": {
            "messageId": "command-00000001",
            "correlationId": HEADERS["X-Correlation-ID"],
            "idempotencyKeySha256": hashlib.sha256(
                HEADERS["Idempotency-Key"].encode()
            ).hexdigest(),
            "sender": "sender@example.com",
            "recipientsSha256": hashlib.sha256(
                json.dumps(
                    ["recipient@example.net"], separators=(",", ":")
                ).encode()
            ).hexdigest(),
        },
    }


@pytest.mark.skipif(not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="PostgreSQL lock test runs in required CI")
def test_concurrent_postgres_replays_commit_one_message(gateway):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client, sessions, _ = gateway
    barrier = Barrier(4)
    def submit(_):
        barrier.wait(timeout=10)
        return client.post("/v1/email/messages", json=document(), headers=HEADERS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))
    assert [response.status_code for response in responses] == [202] * 4
    assert len({response.json()["message_id"] for response in responses}) == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(core.Message)) == 1
        assert session.scalar(select(func.count()).select_from(EmailCommandBinding)) == 1


def test_submit_readback_and_identical_replay_use_one_message(gateway):
    client, sessions, _ = gateway
    payload = document()
    first = client.post("/v1/email/messages", json=payload, headers=HEADERS)
    assert first.status_code == 202, first.text
    value = first.json()
    assert value["command_id"] == payload["message_id"]
    assert value["message_id"] != payload["message_id"]
    assert value["request_hash"] == hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert client.post("/v1/email/messages", json=payload, headers=HEADERS).json() == value
    assert client.get("/v1/email/messages/" + payload["message_id"]).json() == value
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(core.Message)) == 1
        assert session.scalar(select(func.count()).select_from(EmailCommandBinding)) == 1
        assert session.scalar(select(func.count()).select_from(core.EmailOutbox)) == 0


def test_real_auth_resolves_send_and_read_permissions_and_fails_closed(gateway, monkeypatch):
    import httpx
    client, _, _ = gateway
    app.dependency_overrides.pop(core.auth)
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "https://resolver.invalid/resolve")
    permissions = []
    def resolve(url, *, headers, **kwargs):
        permission = headers["X-Codestra-Required-Permission"]
        permissions.append(permission)
        assert headers["X-Klyrow-Tenant-Id"] == "tenant-a"
        return httpx.Response(200, json={"authorized": True, "permission": permission,
            "identity_id": "middleware-a", "tenant_id": "tenant-a", "role": "service"})
    monkeypatch.setattr(core.httpx, "get", resolve)
    headers = {**HEADERS, "Authorization": "Bearer synthetic-service-token", "X-Tenant-ID": "tenant-a"}
    assert client.post("/v1/email/messages", json=document(), headers=HEADERS).status_code == 401
    assert client.post("/v1/email/messages", json=document(), headers=headers).status_code == 202
    assert client.get("/v1/email/messages/command-00000001", headers=headers).status_code == 200
    assert permissions == ["klyrow.send", "klyrow.read"]
    monkeypatch.setattr(core.httpx, "get", lambda *a, **k: httpx.Response(403))
    assert client.get("/v1/email/messages/command-00000001", headers=headers).status_code == 403


@pytest.mark.parametrize("change", ["body", "command", "key", "correlation"])
def test_changed_identity_or_content_conflicts(gateway, change):
    client, _, _ = gateway
    assert client.post("/v1/email/messages", json=document(), headers=HEADERS).status_code == 202
    payload, headers = document(), dict(HEADERS)
    if change == "body": payload["text"] = "Different"
    if change == "command": payload["message_id"] = "command-00000002"
    if change == "key": headers["Idempotency-Key"] = "email-command-key-0002"
    if change == "correlation": headers["X-Correlation-ID"] = "email-correlation-0002"
    assert client.post("/v1/email/messages", json=payload, headers=headers).status_code == 409


@pytest.mark.parametrize("field,value", [("tenant", "tenant-b"), ("sub", "middleware-b")])
def test_command_readback_is_tenant_and_caller_scoped(gateway, field, value):
    client, _, context = gateway
    assert client.post("/v1/email/messages", json=document(), headers=HEADERS).status_code == 202
    context[field] = value
    assert client.get("/v1/email/messages/command-00000001").status_code == 404


def test_suppression_denial_rolls_back_the_command_binding(gateway):
    client, sessions, _ = gateway
    with sessions() as session:
        session.add(core.Suppression(id="blocked", tenant_id="tenant-a", email="recipient@example.net", reason="hard_bounce"))
        session.commit()
    assert client.post("/v1/email/messages", json=document(), headers=HEADERS).status_code == 422
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(EmailCommandBinding)) == 0
        assert session.scalar(select(func.count()).select_from(core.Message)) == 0


@pytest.mark.parametrize("changes", [
    {"recipients": ["a@example.net", "b@example.net"]},
    {"template_id": "unimplemented"}, {"scheduled_at": "2026-12-01T00:00:00Z"},
    {"stream": "marketing"},
])
def test_unsupported_commands_fail_before_acceptance(gateway, changes):
    client, _, _ = gateway
    assert client.post("/v1/email/messages", json=document(**changes), headers=HEADERS).status_code == 422


def test_read_only_and_non_service_principals_cannot_send(gateway):
    client, _, context = gateway
    context["permissions"] = ["klyrow.read"]
    assert client.post("/v1/email/messages", json=document(), headers=HEADERS).status_code == 403
    context.update(service=False, role="tenant_admin")
    assert client.post("/v1/email/messages", json=document(), headers=HEADERS).status_code == 403


def test_live_middleware_route_requires_production_authorization(gateway, monkeypatch):
    client, _, _ = gateway
    monkeypatch.setattr(core, "SAFE_MODE", False)
    monkeypatch.setenv("KLYROW_MIDDLEWARE_EMAIL_IDENTITY", "middleware-a")
    response = client.post("/v1/email/messages", json=document(), headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["detail"] == "production_authorization_invalid"


def test_live_middleware_route_requires_exact_service_identity(gateway, monkeypatch):
    client, _, _ = gateway
    monkeypatch.setattr(core, "SAFE_MODE", False)
    monkeypatch.setenv("KLYROW_MIDDLEWARE_EMAIL_IDENTITY", "different-service")
    response = client.post("/v1/email/messages", json=document(), headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["detail"] == "middleware_email_identity_denied"


def test_live_authorized_command_bypasses_only_the_consumed_canary(gateway, monkeypatch):
    client, sessions, _ = gateway
    from apps.gateway.app.billing import BillingSubscription

    with sessions() as session:
        session.add(BillingSubscription(
            id="subscription-a",
            tenant_id="tenant-a",
            plan_id="production-plan",
            price_id="production-price",
            status="ACTIVE",
            period_end=datetime.now(timezone.utc) + timedelta(days=30),
        ))
        session.commit()
    monkeypatch.setattr(core, "SAFE_MODE", False)
    monkeypatch.setenv("KLYROW_MIDDLEWARE_EMAIL_IDENTITY", "middleware-a")
    payload = document(metadata={
        "category": "transactional",
        "productionAuthorization": production_authorization(),
    })
    response = client.post("/v1/email/messages", json=payload, headers=HEADERS)
    assert response.status_code == 202, response.text
    with sessions() as session:
        outbox = session.scalar(select(core.EmailOutbox))
        assert outbox is not None
        stored = json.loads(outbox.payload)
        assert "_codestra_production_authorization" in stored
        assert session.scalar(select(func.count()).select_from(core.ProductionCanaryGate)) == 0


def test_alert_recipient_policy_is_enforced_at_gateway(gateway):
    client, _, _ = gateway
    payload = document(stream="operational", classification="operational-alert",
        sender="alerts@codestra.co", reply_to="appolon1908@gmail.com",
        recipient_policy_id="codestra-observability-admin-v1", sender_policy_id="codestra-alert-sender-v1")
    assert client.post("/v1/email/messages", json=payload, headers=HEADERS).status_code == 403
