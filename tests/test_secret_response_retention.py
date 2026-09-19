from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.gateway.app import main, service_worker
from apps.gateway.app.secret_responses import (
    SecretResponse,
    cleanup_secret_responses,
    read_secret_response,
    record_secret_response,
)


@pytest.fixture
def response_store(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread":False})
    SecretResponse.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "DB", factory)
    monkeypatch.setenv("KLYROW_SECRET_RESPONSE_RETENTION_SECONDS", "86400")
    yield factory
    engine.dispose()


def create_response(session, current, tenant="tenant-a"):
    return record_secret_response(
        session,
        tenant_id=tenant,
        resource_type="API_KEY",
        resource_id="key-a",
        action="CREATE",
        payload={"id":"key-a","secret":"kly_live_fixture"},
        actor="owner-a",
        current=current,
    )


def test_secret_material_remains_encrypted_and_available_before_24_hours(response_store):
    created = datetime(2026, 9, 12, tzinfo=timezone.utc)
    with response_store() as session:
        item = create_response(session, created)
        session.commit()
        assert "kly_live_fixture" not in item.encrypted_payload
        assert cleanup_secret_responses(session, current=created+timedelta(hours=23, minutes=59)) == 0
        assert read_secret_response(item, current=created+timedelta(hours=23))["secret"] == "kly_live_fixture"


def test_expired_secret_is_irrecoverably_redacted_but_metadata_remains(response_store):
    created = datetime(2026, 9, 12, tzinfo=timezone.utc)
    with response_store() as session:
        item = create_response(session, created)
        session.commit()
        response_id, resource_id = item.id, item.resource_id
        assert cleanup_secret_responses(session, current=created+timedelta(hours=24)) == 1
        session.commit()
        assert cleanup_secret_responses(session, current=created+timedelta(days=2)) == 0
        item = session.get(SecretResponse, response_id)
        assert item.resource_id == resource_id
        assert item.encrypted_payload is None and item.redacted_at is not None
        with pytest.raises(HTTPException) as failure:
            read_secret_response(item, current=created+timedelta(days=2))
        assert failure.value.status_code == 410


def test_api_secret_response_is_tenant_scoped_and_unavailable_after_expiry(response_store):
    created = datetime.now(timezone.utc)-timedelta(days=2)
    with response_store() as session:
        item = create_response(session, created)
        session.commit()
        response_id = item.id
    main.app.dependency_overrides[main.auth] = lambda: {"tenant":"tenant-a","sub":"owner-a","role":"tenant_admin"}
    client = TestClient(main.app)
    try:
        response = client.get(f"/v1/secret-responses/{response_id}")
        assert response.status_code == 410
        assert response.json()["detail"] == "secret_response_expired"
        main.app.dependency_overrides[main.auth] = lambda: {"tenant":"tenant-b","sub":"owner-b","role":"tenant_admin"}
        assert client.get(f"/v1/secret-responses/{response_id}").status_code == 404
    finally:
        main.app.dependency_overrides.pop(main.auth, None)


def test_api_secret_response_is_visible_only_to_the_creating_actor(response_store):
    created = datetime.now(timezone.utc)
    with response_store() as session:
        item = create_response(session, created)
        session.commit()
        response_id = item.id
    client = TestClient(main.app)
    try:
        main.app.dependency_overrides[main.auth] = lambda: {
            "tenant": "tenant-a",
            "sub": "other-admin",
            "role": "tenant_admin",
        }
        assert client.get(f"/v1/secret-responses/{response_id}").status_code == 404
        main.app.dependency_overrides[main.auth] = lambda: {
            "tenant": "tenant-a",
            "sub": "owner-a",
            "role": "tenant_admin",
        }
        assert client.get(f"/v1/secret-responses/{response_id}").status_code == 200
    finally:
        main.app.dependency_overrides.pop(main.auth, None)


def test_base_mail_worker_redacts_expired_secret_responses(
    response_store, monkeypatch
):
    created = datetime.now(timezone.utc) - timedelta(days=2)
    with response_store() as session:
        item = create_response(session, created)
        session.commit()
        response_id = item.id
    monkeypatch.setattr(service_worker, "DB", response_store)
    assert service_worker.secret_response_maintenance_tick() == 1
    with response_store() as session:
        item = session.get(SecretResponse, response_id)
        assert item.encrypted_payload is None
        assert item.redacted_at is not None


@pytest.mark.parametrize("seconds", [59, 86401])
def test_retention_configuration_cannot_exceed_24_hours(response_store, monkeypatch, seconds):
    monkeypatch.setenv("KLYROW_SECRET_RESPONSE_RETENTION_SECONDS", str(seconds))
    with response_store() as session, pytest.raises(ValueError, match="invalid_secret_response_retention"):
        create_response(session, datetime.now(timezone.utc))


def test_api_smtp_and_webhook_create_rotate_paths_record_expiring_responses(monkeypatch):
    from apps.gateway.app import messaging, tenancy
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread":False})
    for model in (
        main.Tenant, main.Audit, tenancy.TenantMember, tenancy.ScopedApiKey, tenancy.SmtpCredential,
        messaging.WebhookSubscription, SecretResponse,
    ):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("KLYROW_SECRET_RESPONSE_RETENTION_SECONDS", "86400")
    monkeypatch.setattr(messaging, "safe_webhook_url", lambda value: value)
    ctx = {"tenant":"tenant-a","sub":"owner-a","role":"platform_admin"}
    with factory() as session:
        session.add(main.Tenant(id="tenant-a", name="A"))
        session.commit()
        key = tenancy.api_key_create(tenancy.KeyIn(
            name="Automation", scopes=["mail.send"], environment="staging"
        ), ctx, session)
        old_hash = session.get(tenancy.ScopedApiKey, key["id"]).verifier_hash
        rotated_key = tenancy.api_key_rotate(key["id"], ctx, session)
        assert rotated_key["secret"] != key["secret"]
        assert session.get(tenancy.ScopedApiKey, key["id"]).verifier_hash != old_hash
        smtp = tenancy.smtp_create(tenancy.SmtpIn(), ctx, session)
        rotated_smtp = tenancy.smtp_rotate(smtp["id"], ctx, session)
        assert rotated_smtp["password"] != smtp["password"]
        webhook = messaging.webhook_create(messaging.WebhookIn(
            url="https://hooks.example.com/klyrow", events=["message.delivered"]
        ), ctx, session)
        rotated_webhook = messaging.webhook_rotate(webhook["id"], ctx, session)
        assert rotated_webhook["secret"] != webhook["secret"]
        rows = list(session.query(SecretResponse).order_by(SecretResponse.created_at))
        assert [(row.resource_type, row.action) for row in rows] == [
            ("API_KEY","CREATE"), ("API_KEY","ROTATE"),
            ("SMTP_CREDENTIAL","CREATE"), ("SMTP_CREDENTIAL","ROTATE"),
            ("WEBHOOK_SECRET","CREATE"), ("WEBHOOK_SECRET","ROTATE"),
        ]
        assert all(row.expires_at is not None and row.encrypted_payload for row in rows)
    engine.dispose()
