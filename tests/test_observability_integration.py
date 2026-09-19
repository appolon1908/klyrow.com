from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("KLYROW_DATABASE_URL", "sqlite:///./test-observability.db")
os.environ.setdefault(
    "KLYROW_SESSION_SECRET",
    "test-secret-observability-minimum-32-characters-long",
)
os.environ.setdefault("KLYROW_SAFE_MODE", "true")
os.environ.setdefault("KLYROW_ENV", "test")

# Main registers the observability router; import it before the module alias.
from apps.gateway.app.main import Base, Tenant
from apps.gateway.app.operations import IntegrationOutbox
from apps.gateway.app.platform import app
from apps.gateway.app.production_api import _authorize_operation_mutation
from apps.gateway.app import observability


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "monitoring/kyyow-observability-sync-v1.json"
BOUNDARY_PATH = ROOT / "monitoring/codestra-observability-boundary.v1.json"
SERVICE_CONTEXT = {
    "sub": "middleware-observability",
    "tenant": "tenant-a",
    "identity_type": "SERVICE",
    "service": True,
    "permissions": [
        observability.OBSERVABILITY_READ,
        observability.OBSERVABILITY_WRITE,
    ],
}


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _projection_hash(payload: dict) -> str:
    document = dict(payload)
    document.pop("projection_hash", None)
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _assert_canonical_projection(payload: dict, schema_name: str) -> None:
    schema = _contract()["schemas"][schema_name]
    assert set(payload) == set(schema["required"])
    assert payload["schema_version"] == schema["properties"]["schema_version"]["const"]
    assert payload["projection_hash"] == _projection_hash(payload)
    assert payload["source_payload_hash"].startswith("sha256:")
    assert len(payload["source_payload_hash"]) == 71
    assert "odoo_model" not in payload


@pytest.fixture
def isolated_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session:
        session.add(
            Tenant(
                id="tenant-a",
                name="Tenant A",
                enabled=True,
                quota=10_000,
            )
        )
        session.commit()
        yield session
    engine.dispose()


def _kpi_snapshot(**overrides) -> observability.KpiSnapshotIn:
    payload = {
        "snapshot_id": "snapshot-20260912-1",
        "kpi_key": "http_error_ratio",
        "query_ref": "klyrow:http_errors:ratio5m",
        "value": 0.01,
        "unit": "ratio",
        "observed_at": "2026-09-12T10:00:00Z",
        "window_start": "2026-09-12T09:55:00Z",
        "window_end": "2026-09-12T10:00:00Z",
        "service": "klyrow-gateway",
        "environment": "production",
        "labels": {
            "service": "klyrow-gateway",
            "environment": "production",
        },
    }
    payload.update(overrides)
    return observability.KpiSnapshotIn.model_validate(payload)


def test_observability_routes_are_unique_and_openapi_classified() -> None:
    expected = {
        ("POST", "/v1/internal/integrations/alertmanager/events"),
        ("POST", "/v1/internal/integrations/kpis/snapshots"),
        ("GET", "/v1/internal/integrations/odoo/health"),
        ("GET", "/v1/internal/integrations/odoo/checkpoints"),
        ("GET", "/v1/internal/integrations/observability/contract"),
    }
    effective_routes = [
        context.original_route
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
    ]
    actual = {
        (method, route.path)
        for route in effective_routes
        if route.path in {path for _method, path in expected}
        for method in route.methods or set()
    }
    assert actual == expected
    for method, path in expected:
        assert sum(
            method in (route.methods or set()) and route.path == path
            for route in effective_routes
        ) == 1

    schema = app.openapi()
    for method, path in expected:
        operation = schema["paths"][path][method.lower()]
        assert operation["security"] == [{"serviceBearer": []}]
        assert (
            operation["x-klyrow-auth-model"]
            == "DEDICATED_SERVICE_BEARER_ON_PRIVATE_ROUTE"
        )
        assert operation["x-klyrow-audience"] == "INTERNAL"
    assert "/v1/internal/integrations/odoo/reconcile" not in schema["paths"]


def test_observability_posts_reject_payloads_over_64_kib_before_auth() -> None:
    oversized = b"{" + b"x" * observability.MAX_OBSERVABILITY_REQUEST_BYTES + b"}"
    with TestClient(app) as client:
        response = client.post(
            "/v1/internal/integrations/alertmanager/events",
            content=oversized,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "observability request exceeds 64 KiB"}


def test_projection_hash_matches_pinned_middleware_utf8_canonicalizer() -> None:
    document = {"summary": "queue stalled – café"}
    assert observability._canonical_json(document) == (
        '{"summary":"queue stalled – café"}'
    )
    assert observability._with_projection_hash(document)["projection_hash"] == (
        "sha256:714d7f40faad7cfcc8faa492102a03d027b451ea4fc38f05c91f7b096acfc142"
    )


def test_labels_and_identifiers_fail_closed_before_enqueue() -> None:
    with pytest.raises(HTTPException) as exc:
        observability._normalize_labels({"tenant_id": "tenant-a"})
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        observability._normalize_labels({"unknown_dimension": "value"})
    assert exc.value.status_code == 422

    assert observability._normalize_labels({"service": "x" * 256}) == {
        "service": "x" * 256
    }
    with pytest.raises(HTTPException) as exc:
        observability._normalize_labels(
            {"service": "x" * 129},
            max_value_length=128,
        )
    assert exc.value.status_code == 422
    assert (
        observability._safe_identifier(
            "x",
            "klyrow-gateway:unknown",
            min_length=3,
        )
        == "klyrow-gateway:unknown"
    )

    with pytest.raises(ValueError):
        _kpi_snapshot(snapshot_id="snapshot/invalid")
    with pytest.raises(ValueError):
        _kpi_snapshot(service="service/invalid")


def test_alert_state_projects_exact_incident_contract(isolated_session) -> None:
    firing = observability.AlertmanagerEnvelope.model_validate(
        {
            "receiver": "klyrow-observability",
            "status": "firing",
            "groupKey": "queue-stalled",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "KlyrowEmailQueueStalled",
                        "severity": "critical",
                        "service": "klyrow-gateway",
                        "environment": "production",
                    },
                    "annotations": {"summary": "queue stalled – café"},
                    "startsAt": "2026-09-12T10:00:00Z",
                    "fingerprint": "queue-stalled-fingerprint",
                }
            ],
        }
    )

    first = observability.receive_alertmanager_events(
        firing,
        SERVICE_CONTEXT,
        isolated_session,
        "correlation-1",
    )
    replay = observability.receive_alertmanager_events(
        firing,
        SERVICE_CONTEXT,
        isolated_session,
        "correlation-retry",
    )
    assert first["accepted"] == 1
    assert first["duplicates"] == 0
    assert replay["accepted"] == 0
    assert replay["duplicates"] == 1
    assert replay["operations"] == first["operations"]

    resolved = observability.AlertmanagerEnvelope.model_validate(
        {
            "receiver": "klyrow-observability",
            "status": "resolved",
            "groupKey": "queue-stalled",
            "alerts": [
                {
                    "status": "resolved",
                    "labels": firing.alerts[0].labels,
                    "annotations": {"summary": "queue restored"},
                    "startsAt": "2026-09-12T10:00:00Z",
                    "endsAt": "2026-09-12T10:05:00Z",
                    "fingerprint": "queue-stalled-fingerprint",
                }
            ],
        }
    )
    completed = observability.receive_alertmanager_events(
        resolved,
        SERVICE_CONTEXT,
        isolated_session,
        "correlation-2",
    )
    assert completed["accepted"] == 1

    rows = isolated_session.scalars(
        select(IntegrationOutbox).where(
            IntegrationOutbox.target == observability.MIDDLEWARE_TARGET
        )
    ).all()
    assert len(rows) == 2
    assert {row.event_type for row in rows} == {observability.INCIDENT_OPERATION}

    projections = sorted(
        (json.loads(row.payload_json) for row in rows),
        key=lambda payload: payload["resource_version"],
    )
    for payload in projections:
        _assert_canonical_projection(payload, "incident_state")
    assert [payload["resource_version"] for payload in projections] == [1, 2]
    assert [payload["state"] for payload in projections] == ["firing", "resolved"]
    assert projections[0]["incident_id"] == projections[1]["incident_id"]
    assert projections[0]["group_key"].startswith("am-group-")
    assert "queue-stalled" not in projections[0]["group_key"]
    assert projections[0]["resolved_at"] is None
    assert projections[1]["resolved_at"] == "2026-09-12T10:05:00Z"
    assert projections[0]["correlation_id"] == "correlation-1"
    assert projections[1]["correlation_id"] == "correlation-2"


def test_resolved_alert_rejects_invalid_time_window(isolated_session) -> None:
    body = observability.AlertmanagerEnvelope.model_validate(
        {
            "status": "resolved",
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {"alertname": "ClockTest"},
                    "startsAt": "2026-09-12T10:00:00Z",
                    "endsAt": "2026-09-12T09:59:00Z",
                    "fingerprint": "clock-test-fingerprint",
                }
            ],
        }
    )
    with pytest.raises(HTTPException) as exc:
        observability.receive_alertmanager_events(
            body,
            SERVICE_CONTEXT,
            isolated_session,
            "correlation-clock",
        )
    assert exc.value.detail == "invalid_alert_time_window"


def test_kpi_snapshot_projects_exact_contract_and_is_idempotent(
    isolated_session,
) -> None:
    snapshot = _kpi_snapshot()
    response = observability.receive_kpi_snapshot(
        snapshot,
        SERVICE_CONTEXT,
        isolated_session,
        "correlation-kpi-1",
    )
    replay = observability.receive_kpi_snapshot(
        snapshot,
        SERVICE_CONTEXT,
        isolated_session,
        "correlation-kpi-retry",
    )
    assert response["duplicate"] is False
    assert replay["duplicate"] is True
    assert response["operation_id"] == replay["operation_id"]
    assert response["target"] == "middleware-observability-api"
    assert response["direct_odoo_database_write"] is False

    row = isolated_session.scalar(
        select(IntegrationOutbox).where(
            IntegrationOutbox.id == response["operation_id"]
        )
    )
    assert row is not None
    assert row.target == observability.MIDDLEWARE_TARGET
    assert row.event_type == observability.KPI_OPERATION
    payload = json.loads(row.payload_json)
    _assert_canonical_projection(payload, "kpi_snapshot")
    assert payload["correlation_id"] == "correlation-kpi-1"
    assert payload["period_start"] == "2026-09-12T09:55:00Z"
    assert payload["period_end"] == "2026-09-12T10:00:00Z"

    with pytest.raises(ValueError, match="unapproved_prometheus_query_reference"):
        _kpi_snapshot(
            snapshot_id="snapshot-20260912-2",
            query_ref='up{job="klyrow"}',
        )


@pytest.mark.parametrize("field", ["observed_at", "window_start", "window_end"])
def test_kpi_timestamps_require_explicit_timezone(field: str) -> None:
    values = {
        "observed_at": "2026-09-12T10:00:00Z",
        "window_start": "2026-09-12T09:00:00Z",
        "window_end": "2026-09-12T10:00:00Z",
    }
    values[field] = "2026-09-12T10:00:00"
    with pytest.raises(
        ValueError,
        match="observability_timestamp_requires_timezone",
    ):
        _kpi_snapshot(**values)


def test_worker_posts_unchanged_projection_to_canonical_middleware(
    isolated_session,
    monkeypatch,
) -> None:
    response = observability.receive_kpi_snapshot(
        _kpi_snapshot(),
        SERVICE_CONTEXT,
        isolated_session,
        "correlation-kpi-wire",
    )
    row = isolated_session.get(IntegrationOutbox, response["operation_id"])
    assert row is not None
    observed: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["path"] = request.url.path
        observed["headers"] = dict(request.headers)
        observed["payload"] = json.loads(request.content)
        payload = observed["payload"]
        return httpx.Response(
            202,
            json={
                "status": "accepted",
                "event_id": payload["event_id"],
                "operation": observability.KPI_OPERATION,
                "correlation_id": payload["correlation_id"],
                "delivery_id": "delivery-123",
                "odoo_sync_state": "pending",
                "duplicate": False,
            },
        )

    claim = {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "target": row.target,
        "event_type": row.event_type,
        "payload_json": row.payload_json,
        "idempotency_key": row.idempotency_key,
        "attempt": 1,
    }
    monkeypatch.setenv(
        "KLYROW_OBSERVABILITY_MIDDLEWARE_URL",
        "https://middleware.example",
    )

    async def send() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            return await observability._send_projection(
                claim,
                client=client,
                token="service-token",
            )

    result = asyncio.run(send())
    assert result["delivery_id"] == "delivery-123"
    assert observed["method"] == "POST"
    assert observed["path"] == "/v1/observability/kpis"
    assert observed["headers"]["authorization"] == "Bearer service-token"
    assert observed["headers"]["idempotency-key"] == row.idempotency_key
    assert observed["headers"]["x-correlation-id"] == "correlation-kpi-wire"
    assert observed["headers"]["x-tenant-id"] == "tenant-a"
    assert observed["payload"] == json.loads(row.payload_json)
    _assert_canonical_projection(observed["payload"], "kpi_snapshot")


def test_transport_rejects_redirect_ack(monkeypatch) -> None:
    payload = {
        key: None
        for key in _contract()["schemas"]["kpi_snapshot"]["required"]
    }
    payload.update(
        {
            "event_id": "event-redirect",
            "tenant_id": "tenant-a",
            "correlation_id": "correlation-redirect",
        }
    )
    claim = {
        "id": "outbox-redirect",
        "tenant_id": "tenant-a",
        "target": observability.MIDDLEWARE_TARGET,
        "event_type": observability.KPI_OPERATION,
        "payload_json": json.dumps(payload),
        "idempotency_key": "redirect-test-key",
        "attempt": 1,
    }
    monkeypatch.setenv(
        "KLYROW_OBSERVABILITY_MIDDLEWARE_URL",
        "https://middleware.example",
    )

    async def send() -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                307,
                headers={"location": "https://untrusted.example/"},
            )
        )
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
        ) as client:
            await observability._send_projection(
                claim,
                client=client,
                token="service-token",
            )

    with pytest.raises(observability.ProjectionDeliveryError) as exc:
        asyncio.run(send())
    assert exc.value.code == "middleware_projection_redirect_rejected"
    assert exc.value.retryable is False


def test_cross_repository_contract_and_worker_are_pinned() -> None:
    contract = _contract()
    boundary = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
    canonical = boundary["canonical_projection"]
    raw = CONTRACT_PATH.read_bytes()
    git_blob = hashlib.sha1(
        f"blob {len(raw)}\0".encode("ascii") + raw
    ).hexdigest()

    assert canonical["source_repository"] == "appolon1908-hue/Middleware-"
    assert canonical["source_ref"] == "main"
    assert canonical["source_commit_sha"] == "61d899f98048f4465303cdcb616a4c9f5a35ceb5"
    assert canonical["source_blob_sha"] == git_blob
    assert contract["authority"]["api_owner"] == canonical["source_repository"]
    assert contract["authority"]["business_record_owner"] == "appolon1908-hue/Odoo"
    assert observability.PROJECTION_PATHS == {
        observability.KPI_OPERATION: "/v1/observability/kpis",
        observability.INCIDENT_OPERATION: "/v1/observability/incidents",
    }
    assert {entry["operation"] for entry in canonical["middleware_operations"]} == {
        observability.KPI_OPERATION,
        observability.INCIDENT_OPERATION,
    }
    assert {
        entry["operation"]
        for entry in contract["odoo_endpoints"]
        if entry["method"] == "POST"
    } == {
        "odoo.observability.kpis.create",
        "odoo.observability.incidents.upsert",
    }

    worker = (ROOT / "apps/gateway/app/service_worker.py").read_text(
        encoding="utf-8"
    )
    overlay = (ROOT / "deploy/docker-compose.observability.yml").read_text(
        encoding="utf-8"
    )
    assert 'elif ROLE == "observability":' in worker
    assert "await dispatch_observability_outbox()" in worker
    assert "KLYROW_WORKER_ROLE: observability" in overlay
    assert 'KLYROW_OBSERVABILITY_DELIVERY_ENABLED:-false' in overlay


def test_observability_operation_recovery_requires_target_permission() -> None:
    item = IntegrationOutbox(
        id="permission-test",
        tenant_id="tenant-a",
        target=observability.MIDDLEWARE_TARGET,
        event_type=observability.KPI_OPERATION,
        aggregate_id="metric",
        payload_json="{}",
        idempotency_key="permission-test",
    )
    _authorize_operation_mutation(SERVICE_CONTEXT, item)

    denied = {**SERVICE_CONTEXT, "permissions": []}
    with pytest.raises(HTTPException) as exc:
        _authorize_operation_mutation(denied, item)
    assert exc.value.status_code == 403


def test_status_summaries_are_partitioned_by_tenant(isolated_session) -> None:
    for tenant, state in (("tenant-a", "PENDING"), ("tenant-b", "DEAD_LETTER")):
        isolated_session.add(
            IntegrationOutbox(
                id=tenant,
                tenant_id=tenant,
                target=observability.MIDDLEWARE_TARGET,
                event_type=observability.KPI_OPERATION,
                aggregate_id=tenant,
                payload_json="{}",
                idempotency_key=tenant,
                state=state,
            )
        )
    isolated_session.commit()
    assert observability._outbox_summary(
        isolated_session,
        "tenant-a",
    )["counts"] == {"PENDING": 1}
    assert observability._outbox_summary(isolated_session, "empty")["counts"] == {}
    checkpoints = observability.odoo_checkpoints(
        SERVICE_CONTEXT,
        isolated_session,
    )
    assert [
        (item["state"], item["count"])
        for item in checkpoints["items"]
    ] == [("PENDING", 1)]
