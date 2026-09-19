"""PostgreSQL proofs for the canonical observability outbox and relay."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Barrier
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("KLYROW_DATABASE_URL", "sqlite:///./test-observability-pg-import.db")
os.environ.setdefault(
    "KLYROW_SESSION_SECRET",
    "test-observability-postgres-session-secret-32",
)
os.environ.setdefault("KLYROW_SAFE_MODE", "true")
os.environ.setdefault("KLYROW_ENV", "test")

from apps.gateway.app.main import Audit, Tenant
from apps.gateway.app.operations import IntegrationOutbox, IntegrationResult
from apps.gateway.app.platform import app  # noqa: F401
from apps.gateway.app import observability


pytestmark = pytest.mark.skipif(
    not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"),
    reason="Requires disposable PostgreSQL",
)
CONTEXT = {
    "sub": "postgres-observability-test",
    "tenant": "tenant-a",
    "identity_type": "SERVICE",
    "service": True,
    "permissions": [
        observability.OBSERVABILITY_READ,
        observability.OBSERVABILITY_WRITE,
    ],
}


def _firing() -> observability.AlertmanagerEnvelope:
    return observability.AlertmanagerEnvelope.model_validate(
        {
            "status": "firing",
            "groupKey": "postgres-concurrency",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "PostgresConcurrency",
                        "severity": "warning",
                        "service": "klyrow-gateway",
                        "environment": "test",
                    },
                    "annotations": {"summary": "PostgreSQL serialization"},
                    "startsAt": "2026-09-13T12:00:00Z",
                    "fingerprint": "postgres-concurrency-fingerprint",
                }
            ],
        }
    )


@pytest.fixture
def postgres_sessions(monkeypatch):
    url = os.environ["KLYROW_CONTRACT_POSTGRES_URL"]
    admin = create_engine(url)
    schema = "observability_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=5,
    )
    for model in (Tenant, Audit, IntegrationOutbox, IntegrationResult):
        model.__table__.create(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session:
        session.add(Tenant(id="tenant-a", name="Tenant A", quota=10_000))
        session.commit()
    monkeypatch.setattr(observability, "DB", sessions)
    try:
        yield sessions
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_concurrent_incident_replay_serializes_to_one_row(
    postgres_sessions,
) -> None:
    barrier = Barrier(2)
    body = _firing()

    def submit(_index: int) -> dict:
        with postgres_sessions() as session:
            barrier.wait(timeout=10)
            return observability.receive_alertmanager_events(
                body,
                CONTEXT,
                session,
                "correlation-postgres",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))

    assert sum(result["accepted"] for result in results) == 1
    assert sum(result["duplicates"] for result in results) == 1
    assert len({result["operations"][0] for result in results}) == 1
    with postgres_sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(IntegrationOutbox)
        ) == 1
        payload = json.loads(session.scalar(select(IntegrationOutbox)).payload_json)
        assert payload["resource_version"] == 1


def test_postgres_claim_and_completion_are_fenced_and_durable(
    postgres_sessions,
) -> None:
    with postgres_sessions() as session:
        response = observability.receive_alertmanager_events(
            _firing(),
            CONTEXT,
            session,
            "correlation-postgres-worker",
        )
        operation_id = response["operations"][0]

    claim = observability._claim_projection()
    assert claim is not None
    assert claim["id"] == operation_id
    assert claim["attempt"] == 1
    with postgres_sessions() as session:
        claimed = session.get(IntegrationOutbox, operation_id)
        assert claimed.state == "PROCESSING"
        assert claimed.lease_expires_at is not None

    observability._complete_projection(
        claim,
        {
            "status": "accepted",
            "event_id": json.loads(claim["payload_json"])["event_id"],
            "operation": observability.INCIDENT_OPERATION,
            "correlation_id": "correlation-postgres-worker",
            "delivery_id": "postgres-delivery-1",
            "odoo_sync_state": "pending",
            "duplicate": False,
        },
    )
    with postgres_sessions() as session:
        completed = session.get(IntegrationOutbox, operation_id)
        assert completed.state == "COMPLETED"
        assert completed.lease_expires_at is None
        result = session.scalar(
            select(IntegrationResult).where(
                IntegrationResult.outbox_id == operation_id
            )
        )
        assert result is not None
        assert result.source == observability.MIDDLEWARE_TARGET

    # A stale completion from an older fence cannot overwrite terminal state.
    observability._fail_projection(
        claim,
        observability.ProjectionDeliveryError(
            "stale-worker",
            retryable=True,
        ),
    )
    with postgres_sessions() as session:
        assert session.get(IntegrationOutbox, operation_id).state == "COMPLETED"
        assert session.scalar(
            select(func.count()).select_from(IntegrationResult)
        ) == 1


def test_postgres_permanent_failure_dead_letters_without_null_schedule(
    postgres_sessions,
) -> None:
    with postgres_sessions() as session:
        operation_id = observability.receive_alertmanager_events(
            _firing(),
            CONTEXT,
            session,
            "correlation-postgres-failure",
        )["operations"][0]

    claim = observability._claim_projection()
    assert claim is not None
    observability._fail_projection(
        claim,
        observability.ProjectionDeliveryError(
            "middleware_projection_http_422",
            retryable=False,
        ),
    )

    with postgres_sessions() as session:
        item = session.get(IntegrationOutbox, operation_id)
        assert item.state == "DEAD_LETTER"
        assert item.next_attempt_at is not None
        assert item.lease_expires_at is None
        assert item.last_error == "middleware_projection_http_422"
