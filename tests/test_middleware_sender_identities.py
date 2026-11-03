"""Exercise the service-authenticated sender-identity provisioning routes."""

import os

os.environ.setdefault("KLYROW_DATABASE_URL", "sqlite:///./test-middleware-sender-identities.db")
os.environ.setdefault("KLYROW_SESSION_SECRET", "test-middleware-sender-identities-secret-32")
os.environ.setdefault("KLYROW_SAFE_MODE", "true")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.gateway.app import main as core
from apps.gateway.app.messaging import DomainClaim
from apps.gateway.app.platform import app

DOMAIN_CLAIM_ID = "claim-tenant-a"


@pytest.fixture
def gateway(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    core.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    context = {
        "tenant": "tenant-a",
        "sub": "middleware-service",
        "service": True,
        "role": "tenant_admin",
        "identity_type": "SERVICE",
        "permissions": ["klyrow.middleware.command.write", "klyrow.integration.result.write"],
    }
    with sessions() as session:
        session.add(core.Tenant(id="tenant-a", name="tenant-a", quota=100))
        session.flush()
        session.add(
            DomainClaim(
                id=DOMAIN_CLAIM_ID,
                tenant_id="tenant-a",
                domain="codestra.agency",
                state="VERIFIED",
                challenge_hash="x",
                dkim_selector="s1",
                return_path="bounce.codestra.agency",
                tracking_domain="track.codestra.agency",
            )
        )
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


def body(**changes):
    return {
        "domain_claim_id": DOMAIN_CLAIM_ID,
        "email": "maria.transport@codestra.agency",
        "display_name": "Maria Lopez (Transportation)",
        "stream": "transactional",
        **changes,
    }


def test_create_sender_identity_succeeds(gateway):
    client, _sessions, _ctx = gateway
    response = client.post("/v1/internal/sender-identities", json=body())
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["address"] == "maria.transport@codestra.agency"
    assert payload["status"] == "ACTIVE"
    assert payload["verified"] is True


def test_create_sender_identity_is_idempotent(gateway):
    client, _sessions, _ctx = gateway
    first = client.post("/v1/internal/sender-identities", json=body()).json()
    second = client.post("/v1/internal/sender-identities", json=body()).json()
    assert first["id"] == second["id"]


def test_create_sender_identity_rejects_spoofed_domain(gateway):
    client, _sessions, _ctx = gateway
    response = client.post(
        "/v1/internal/sender-identities", json=body(email="maria@not-codestra.example")
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "sender_spoofing_denied"


def test_create_sender_identity_requires_verified_domain(gateway):
    client, sessions, _ctx = gateway
    with sessions() as session:
        claim = session.get(DomainClaim, DOMAIN_CLAIM_ID)
        claim.state = "DNS_REQUIRED"
        session.commit()
    response = client.post("/v1/internal/sender-identities", json=body())
    assert response.status_code == 409
    assert response.json()["detail"] == "verified_domain_required"


def test_readback_and_disable_round_trip(gateway):
    client, _sessions, _ctx = gateway
    created = client.post("/v1/internal/sender-identities", json=body()).json()
    read = client.get(f"/v1/internal/sender-identities/{created['id']}")
    assert read.status_code == 200
    assert read.json()["status"] == "ACTIVE"
    disabled = client.post(f"/v1/internal/sender-identities/{created['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "DISABLED"
    after = client.get(f"/v1/internal/sender-identities/{created['id']}")
    assert after.json()["status"] == "DISABLED"


def test_non_service_caller_is_rejected(gateway):
    client, _sessions, ctx = gateway
    app.dependency_overrides[core.auth] = lambda: {**ctx, "service": False}
    response = client.post("/v1/internal/sender-identities", json=body())
    assert response.status_code == 403
    assert response.json()["detail"] == "middleware_service_identity_required"


def test_read_sender_identity_is_audited(gateway):
    client, sessions, ctx = gateway
    created = client.post("/v1/internal/sender-identities", json=body()).json()
    client.get(f"/v1/internal/sender-identities/{created['id']}")
    with sessions() as session:
        rows = (
            session.query(core.Audit)
            .filter(
                core.Audit.action == "sender_identity.read",
                core.Audit.tenant_id == ctx["tenant"],
            )
            .all()
        )
    assert len(rows) >= 1
    assert rows[0].actor == ctx["sub"]


def test_sender_identity_reads_are_rate_limited(gateway, monkeypatch):
    client, _sessions, _ctx = gateway
    monkeypatch.setenv("KLYROW_SENDER_IDENTITY_RATE_PER_MINUTE", "3")
    core.rate_buckets.clear()
    created = client.post("/v1/internal/sender-identities", json=body()).json()

    for _ in range(3):
        response = client.get(f"/v1/internal/sender-identities/{created['id']}")
        assert response.status_code == 200

    limited = client.get(f"/v1/internal/sender-identities/{created['id']}")
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate_limit_exceeded"
