"""Canonical observability ingress and durable Middleware delivery.

Klyrow validates bounded Alertmanager and KPI inputs, then persists the exact
payload accepted by Middleware's governed observability API. A separately
enabled worker relays only those canonical operations over mTLS with a
short-lived OIDC service token. Middleware remains the sole component that
creates the Odoo delivery record and writes Odoo.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .capabilities import has_service_permission
from .durable_results import seal_integration_result
from .main import DB, Tenant, audit, auth, db, runtime_secret
from .operations import IntegrationOutbox, IntegrationResult


MAX_OBSERVABILITY_REQUEST_BYTES = 64 * 1024


class BoundedObservabilityRoute(APIRoute):
    """Reject oversized observability writes before model validation."""

    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def bounded_route_handler(request: Request):
            if request.method == "POST":
                content_length = request.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_OBSERVABILITY_REQUEST_BYTES:
                            raise HTTPException(
                                413,
                                "observability request exceeds 64 KiB",
                            )
                    except ValueError:
                        pass

                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_OBSERVABILITY_REQUEST_BYTES:
                        raise HTTPException(
                            413,
                            "observability request exceeds 64 KiB",
                        )
                request._body = bytes(body)

            return await route_handler(request)

        return bounded_route_handler


router = APIRouter(
    prefix="/v1/internal/integrations",
    tags=["Observability integrations"],
    route_class=BoundedObservabilityRoute,
)

OBSERVABILITY_READ = "klyrow.observability.read"
OBSERVABILITY_WRITE = "klyrow.observability.write"
MIDDLEWARE_TARGET = "MIDDLEWARE_OBSERVABILITY"
KPI_OPERATION = "observability.kpis.create"
INCIDENT_OPERATION = "observability.incidents.upsert"
PROJECTION_OPERATIONS = frozenset({KPI_OPERATION, INCIDENT_OPERATION})
PROJECTION_PATHS = {
    KPI_OPERATION: "/v1/observability/kpis",
    INCIDENT_OPERATION: "/v1/observability/incidents",
}
MAX_DELIVERY_ATTEMPTS = 5

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
FORBIDDEN_LABELS = frozenset(
    {
        "tenant_id",
        "customer_id",
        "organization_id",
        "user_id",
        "email",
        "phone",
        "recipient",
        "sender",
        "message_id",
        "trace_id",
        "request_id",
        "raw_url",
        "query_string",
        "idempotency_key",
        "smtp_username",
    }
)
ALLOWED_LABEL_KEY = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
ALLOWED_LABELS = frozenset(
    {
        "codestra_business",
        "application",
        "alertname",
        "service",
        "environment",
        "server",
        "region",
        "deployment",
        "job",
        "instance",
        "target",
        "state",
        "status",
        "severity",
        "outcome",
    }
)
PROMETHEUS_KPI_REFERENCES = frozenset(
    {
        "klyrow:http_requests:rate5m",
        "klyrow:http_errors:ratio5m",
        "klyrow:mail_events:rate15m",
        "klyrow:provider_queue:sum",
        "klyrow:integration_outbox:sum",
        "klyrow:email_outbox_oldest_seconds:max",
    }
)
KPI_RECONCILIATION_STATE = {
    "observed": "accepted",
    "stale": "pending",
    "unavailable": "drifted",
}


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict, max_length=32)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=20)
    starts_at: str = Field(alias="startsAt", min_length=1, max_length=80)
    ends_at: str = Field(alias="endsAt", default="", max_length=80)
    generator_url: str = Field(alias="generatorURL", default="", max_length=2000)
    fingerprint: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]+$",
    )


class AlertmanagerEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    receiver: str = Field(default="", max_length=200)
    status: Literal["firing", "resolved"] = "firing"
    alerts: list[AlertmanagerAlert] = Field(min_length=1, max_length=100)
    group_key: str = Field(alias="groupKey", default="", max_length=2048)
    external_url: str = Field(alias="externalURL", default="", max_length=2000)


class KpiSnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$",
    )
    kpi_key: str = Field(
        min_length=3,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_.:-]*$",
    )
    query_ref: str = Field(min_length=3, max_length=96)
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(
        default="ratio",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z][A-Za-z0-9_.%/-]{0,31}$",
    )
    observed_at: datetime
    window_start: datetime
    window_end: datetime
    service: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    environment: Literal["development", "test", "staging", "production"]
    labels: dict[str, str] = Field(default_factory=dict, max_length=32)
    status: Literal["observed", "stale", "unavailable"] = "observed"
    source_revision: int = Field(default=1, ge=1)

    @field_validator("observed_at", "window_start", "window_end")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observability_timestamp_requires_timezone")
        return value

    @field_validator("query_ref")
    @classmethod
    def vetted_query_reference(cls, value: str) -> str:
        if value not in PROMETHEUS_KPI_REFERENCES:
            raise ValueError("unapproved_prometheus_query_reference")
        return value

    @model_validator(mode="after")
    def valid_window(self) -> "KpiSnapshotIn":
        if self.window_end <= self.window_start:
            raise ValueError("invalid_kpi_window")
        return self


class ProjectionDeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _require_observability(permission: str):
    def dependency(ctx: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if not has_service_permission(ctx, permission):
            raise HTTPException(403, "observability_service_scope_required")
        return ctx

    dependency.__name__ = "require_observability_" + permission.rsplit(".", 1)[-1]
    return dependency


require_observability_read = _require_observability(OBSERVABILITY_READ)
require_observability_write = _require_observability(OBSERVABILITY_WRITE)


def _normalize_labels(
    labels: dict[str, str],
    *,
    max_value_length: int = 256,
) -> dict[str, str]:
    if len(labels) > 32:
        raise HTTPException(422, "observability_label_limit_exceeded")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in labels.items():
        key = str(raw_key).strip()
        if key.lower() in FORBIDDEN_LABELS:
            raise HTTPException(422, "forbidden_observability_label")
        if key not in ALLOWED_LABELS or not ALLOWED_LABEL_KEY.fullmatch(key):
            raise HTTPException(422, "unapproved_observability_label")
        value = str(raw_value)
        if len(value) > max_value_length:
            raise HTTPException(422, "observability_label_value_too_long")
        normalized[key] = value
    return dict(sorted(normalized.items()))


def _normal_time(value: str | datetime) -> str:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except ValueError as exc:
        raise HTTPException(422, "invalid_observability_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTTPException(422, "observability_timestamp_requires_timezone")
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _safe_annotations(annotations: dict[str, str]) -> dict[str, str]:
    allowed = {"summary", "description", "runbook_url", "dashboard_url"}
    result: dict[str, str] = {}
    for key in allowed:
        value = annotations.get(key)
        if value is not None:
            result[key] = str(value)[:4000]
    return result


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _payload_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _with_projection_hash(payload: dict[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value["projection_hash"] = "sha256:" + _payload_hash(value)
    return value


def _semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"correlation_id", "projection_hash"}
    }


def _safe_identifier(
    value: str,
    fallback: str,
    *,
    min_length: int = 1,
) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(value or "").strip())[:128]
    return (
        candidate
        if len(candidate) >= min_length and IDENTIFIER_RE.fullmatch(candidate)
        else fallback
    )


def _opaque_group_key(value: str, fingerprint: str) -> str:
    return "am-group-" + _payload_hash(value or fingerprint)[:32]


def _correlation_id(value: str, identity: str) -> str:
    candidate = value.strip()
    if candidate:
        if not IDENTIFIER_RE.fullmatch(candidate):
            raise HTTPException(422, "invalid_observability_correlation_id")
        return candidate
    return "corr-" + _payload_hash(identity)[:32]


def _prior_projection(
    session: Session,
    tenant_id: str,
    idempotency_key: str,
) -> IntegrationOutbox | None:
    return session.scalar(
        select(IntegrationOutbox).where(
            IntegrationOutbox.tenant_id == tenant_id,
            IntegrationOutbox.target == MIDDLEWARE_TARGET,
            IntegrationOutbox.idempotency_key == idempotency_key,
        )
    )


def _enqueue_projection(
    session: Session,
    context: dict[str, Any],
    *,
    event_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> tuple[IntegrationOutbox, bool]:
    """Persist one payload accepted verbatim by Middleware's canonical API."""

    if event_type not in PROJECTION_OPERATIONS:
        raise RuntimeError("unsupported_observability_projection_operation")
    if not IDENTIFIER_RE.fullmatch(str(context.get("tenant", ""))):
        raise HTTPException(403, "observability_tenant_scope_invalid")
    payload_json = _canonical_json(payload)
    prior = _prior_projection(session, context["tenant"], idempotency_key)
    if prior is not None:
        if _semantic_payload(json.loads(prior.payload_json)) != _semantic_payload(
            payload
        ):
            raise HTTPException(409, "observability_idempotency_conflict")
        return prior, True

    item = IntegrationOutbox(
        id=str(uuid.uuid4()),
        tenant_id=context["tenant"],
        target=MIDDLEWARE_TARGET,
        event_type=event_type,
        aggregate_id=aggregate_id,
        payload_json=payload_json,
        idempotency_key=idempotency_key,
    )
    try:
        with session.begin_nested():
            session.add(item)
            session.flush()
    except IntegrityError:
        prior = _prior_projection(session, context["tenant"], idempotency_key)
        if prior is None:
            raise
        if _semantic_payload(json.loads(prior.payload_json)) != _semantic_payload(
            payload
        ):
            raise HTTPException(409, "observability_idempotency_conflict")
        return prior, True
    return item, False


def _lock_tenant(session: Session, tenant_id: str) -> None:
    tenant = session.scalar(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()
    )
    if tenant is None:
        raise HTTPException(404, "tenant_not_found")


def _latest_incident_projection(
    session: Session,
    tenant_id: str,
    incident_id: str,
) -> dict[str, Any] | None:
    row = session.scalar(
        select(IntegrationOutbox)
        .where(
            IntegrationOutbox.tenant_id == tenant_id,
            IntegrationOutbox.target == MIDDLEWARE_TARGET,
            IntegrationOutbox.event_type == INCIDENT_OPERATION,
            IntegrationOutbox.aggregate_id == incident_id,
        )
        .order_by(
            IntegrationOutbox.created_at.desc(),
            IntegrationOutbox.id.desc(),
        )
        .limit(1)
    )
    if row is None:
        return None
    try:
        payload = json.loads(row.payload_json)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("resource_version"), int)
        ):
            raise ValueError
        return payload
    except (TypeError, ValueError):
        raise HTTPException(503, "observability_projection_state_invalid") from None


def _alert_payload(
    envelope: AlertmanagerEnvelope,
    alert: AlertmanagerAlert,
    *,
    tenant_id: str,
    correlation_id: str,
    event_id: str,
    incident_id: str,
    resource_version: int,
) -> dict[str, Any]:
    labels = _normalize_labels(alert.labels)
    severity = labels.get("severity", "warning")
    if severity not in {"info", "warning", "high", "critical"}:
        raise HTTPException(422, "invalid_alert_severity")
    environment = labels.get("environment", os.getenv("KLYROW_ENV", "development"))
    if environment not in {"development", "test", "staging", "production"}:
        raise HTTPException(422, "invalid_observability_environment")
    starts_at = _normal_time(alert.starts_at)
    ends_at = _normal_time(alert.ends_at) if alert.ends_at else None
    if alert.status == "resolved" and ends_at is None:
        raise HTTPException(422, "resolved_alert_requires_end_time")
    if ends_at is not None and ends_at < starts_at:
        raise HTTPException(422, "invalid_alert_time_window")
    if alert.status != "resolved" and ends_at is not None:
        raise HTTPException(422, "firing_alert_cannot_have_end_time")
    alertname = _safe_identifier(
        labels.get("alertname", "observability_incident"),
        "observability_incident",
    )
    service_id = _safe_identifier(
        labels.get("service", "klyrow-gateway"),
        "klyrow-gateway",
    )
    source_deployment = _safe_identifier(
        labels.get("deployment")
        or os.getenv("CODESTRA_DEPLOYMENT", "klyrow-gateway:unknown"),
        "klyrow-gateway:unknown",
        min_length=3,
    )
    annotations = _safe_annotations(alert.annotations)
    summary = (
        annotations.get("summary")
        or annotations.get("description")
        or alertname
    ).strip()[:512]
    if not summary:
        summary = alertname
    observed_at = ends_at if alert.status == "resolved" else starts_at
    group_key = _opaque_group_key(envelope.group_key, alert.fingerprint)
    source_document = {
        "status": alert.status,
        "fingerprint": alert.fingerprint,
        "group_key": group_key,
        "labels": labels,
        "annotations": annotations,
        "starts_at": starts_at,
        "ends_at": ends_at,
    }
    return _with_projection_hash(
        {
            "event_id": event_id,
            "schema_version": "kyyow.observability.incident.v1",
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "fingerprint": alert.fingerprint,
            "alertname": alertname,
            "group_key": group_key,
            "severity": severity,
            "state": alert.status,
            "service_id": service_id,
            "environment": environment,
            "host": str(labels.get("instance") or labels.get("server") or "")[:128],
            "summary": summary,
            "labels": labels,
            "first_seen_at": starts_at,
            "last_seen_at": observed_at,
            "resolved_at": ends_at,
            "source_deployment": source_deployment,
            "resource_version": resource_version,
            "source_payload_hash": "sha256:" + _payload_hash(source_document),
            "observed_at": observed_at,
            "correlation_id": correlation_id,
        }
    )


@router.post("/alertmanager/events", status_code=202)
def receive_alertmanager_events(
    body: AlertmanagerEnvelope,
    ctx: dict[str, Any] = Depends(require_observability_write),
    session: Session = Depends(db),
    x_correlation_id: str = Header(
        default="",
        alias="X-Correlation-ID",
        max_length=128,
    ),
) -> dict[str, Any]:
    _lock_tenant(session, ctx["tenant"])
    accepted: list[str] = []
    duplicates = 0
    for alert in body.alerts:
        starts_at = _normal_time(alert.starts_at)
        group_key = _opaque_group_key(body.group_key, alert.fingerprint)
        identity = (
            f"{alert.fingerprint}:{alert.status}:{starts_at}:{group_key}"
        )
        idempotency_key = "observability:incident:" + _payload_hash(identity)
        prior = _prior_projection(session, ctx["tenant"], idempotency_key)
        incident_id = "incident-" + _payload_hash(
            ctx["tenant"] + "\0" + alert.fingerprint
        )[:32]
        if prior is not None:
            prior_payload = json.loads(prior.payload_json)
            correlation = str(prior_payload["correlation_id"])
            resource_version = int(prior_payload["resource_version"])
        else:
            latest = _latest_incident_projection(
                session,
                ctx["tenant"],
                incident_id,
            )
            resource_version = (
                int(latest["resource_version"]) + 1 if latest is not None else 1
            )
            correlation = _correlation_id(x_correlation_id, identity)
        event_id = "incident-event-" + _payload_hash(idempotency_key)[:32]
        payload = _alert_payload(
            body,
            alert,
            tenant_id=ctx["tenant"],
            correlation_id=correlation,
            event_id=event_id,
            incident_id=incident_id,
            resource_version=resource_version,
        )
        item, duplicate = _enqueue_projection(
            session,
            ctx,
            event_type=INCIDENT_OPERATION,
            aggregate_id=incident_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        accepted.append(item.id)
        duplicates += int(duplicate)
    audit(session, ctx, "observability.alertmanager.received")
    session.commit()
    return {
        "accepted": len(accepted) - duplicates,
        "duplicates": duplicates,
        "operations": accepted,
        "target": "middleware-observability-api",
        "direct_odoo_database_write": False,
    }


def _kpi_payload(
    body: KpiSnapshotIn,
    *,
    tenant_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    labels = _normalize_labels(body.labels, max_value_length=128)
    source_document = {
        "snapshot_id": body.snapshot_id,
        "kpi_key": body.kpi_key,
        "query_ref": body.query_ref,
        "value": body.value,
        "unit": body.unit,
        "observed_at": _normal_time(body.observed_at),
        "window_start": _normal_time(body.window_start),
        "window_end": _normal_time(body.window_end),
        "service": body.service,
        "environment": body.environment,
        "labels": labels,
        "status": body.status,
        "source_revision": body.source_revision,
    }
    return _with_projection_hash(
        {
            "event_id": body.snapshot_id,
            "schema_version": "kyyow.observability.kpi.v1",
            "tenant_id": tenant_id,
            "metric_code": body.kpi_key,
            "service_id": body.service,
            "environment": body.environment,
            "period_reference": body.snapshot_id,
            "period_start": source_document["window_start"],
            "period_end": source_document["window_end"],
            "value": body.value,
            "unit": body.unit,
            "dimensions": labels,
            "source": body.query_ref,
            "source_revision": body.source_revision,
            "source_payload_hash": "sha256:" + _payload_hash(source_document),
            "observed_at": source_document["observed_at"],
            "reconciliation_state": KPI_RECONCILIATION_STATE[body.status],
            "correlation_id": correlation_id,
        }
    )


@router.post("/kpis/snapshots", status_code=202)
def receive_kpi_snapshot(
    body: KpiSnapshotIn,
    ctx: dict[str, Any] = Depends(require_observability_write),
    session: Session = Depends(db),
    x_correlation_id: str = Header(
        default="",
        alias="X-Correlation-ID",
        max_length=128,
    ),
) -> dict[str, Any]:
    idempotency_key = "observability:kpi:" + body.snapshot_id
    prior = _prior_projection(session, ctx["tenant"], idempotency_key)
    correlation = (
        str(json.loads(prior.payload_json)["correlation_id"])
        if prior is not None
        else _correlation_id(x_correlation_id, body.snapshot_id)
    )
    payload = _kpi_payload(
        body,
        tenant_id=ctx["tenant"],
        correlation_id=correlation,
    )
    item, duplicate = _enqueue_projection(
        session,
        ctx,
        event_type=KPI_OPERATION,
        aggregate_id=body.kpi_key,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    audit(session, ctx, "observability.kpi_snapshot.received")
    session.commit()
    return {
        "operation_id": item.id,
        "snapshot_id": body.snapshot_id,
        "duplicate": duplicate,
        "state": item.state,
        "target": "middleware-observability-api",
        "direct_odoo_database_write": False,
    }


def _outbox_summary(session: Session, tenant_id: str) -> dict[str, Any]:
    rows = session.execute(
        select(IntegrationOutbox.state, func.count())
        .where(
            IntegrationOutbox.target == MIDDLEWARE_TARGET,
            IntegrationOutbox.tenant_id == tenant_id,
        )
        .group_by(IntegrationOutbox.state)
    ).all()
    counts = {str(state): int(count) for state, count in rows}
    oldest = session.scalar(
        select(IntegrationOutbox.created_at)
        .where(
            IntegrationOutbox.target == MIDDLEWARE_TARGET,
            IntegrationOutbox.tenant_id == tenant_id,
            IntegrationOutbox.state.in_(("PENDING", "PROCESSING", "RETRY")),
        )
        .order_by(IntegrationOutbox.created_at)
        .limit(1)
    )
    oldest_seconds = 0.0
    if oldest:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        oldest_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - oldest).total_seconds(),
        )
    return {
        "counts": counts,
        "oldest_pending_seconds": round(oldest_seconds, 3),
    }


def _configured_file(name: str) -> bool:
    value = os.getenv(name, "").strip()
    if not value:
        return False
    path = Path(value)
    return path.is_absolute() and not path.is_symlink() and path.is_file()


def _valid_https_url(value: str, *, origin_only: bool) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and (not origin_only or parsed.path in {"", "/"})
    )


def _delivery_enabled() -> bool:
    return (
        os.getenv("KLYROW_OBSERVABILITY_DELIVERY_ENABLED", "false")
        .strip()
        .lower()
        == "true"
    )


def _transport_configured() -> bool:
    issuer = os.getenv(
        "CODESTRA_AUTH_ISSUER",
        "https://auth.codestra.co/realms/codestra",
    ).strip()
    return bool(
        _valid_https_url(
            os.getenv("KLYROW_OBSERVABILITY_MIDDLEWARE_URL", "").strip(),
            origin_only=True,
        )
        and _valid_https_url(issuer, origin_only=False)
        and os.getenv("CODESTRA_KEYCLOAK_CLIENT_ID", "").strip()
        and os.getenv("CODESTRA_AUTH_AUDIENCE", "").strip()
        and _configured_file("KLYROW_MIDDLEWARE_CLIENT_SECRET_FILE")
        and _configured_file("KLYROW_SERVER_A_CA_FILE")
        and _configured_file("KLYROW_SERVER_A_CLIENT_CERT_FILE")
        and _configured_file("KLYROW_SERVER_A_CLIENT_KEY_FILE")
    )


def _middleware_base_url() -> str:
    value = os.getenv("KLYROW_OBSERVABILITY_MIDDLEWARE_URL", "").strip().rstrip("/")
    if not _valid_https_url(value, origin_only=True):
        raise ProjectionDeliveryError(
            "middleware_origin_invalid",
            retryable=False,
        )
    return value


def _middleware_tls_context() -> ssl.SSLContext:
    paths = {
        "ca": os.getenv("KLYROW_SERVER_A_CA_FILE", "").strip(),
        "cert": os.getenv("KLYROW_SERVER_A_CLIENT_CERT_FILE", "").strip(),
        "key": os.getenv("KLYROW_SERVER_A_CLIENT_KEY_FILE", "").strip(),
    }
    if not all(paths.values()):
        raise ProjectionDeliveryError(
            "middleware_mtls_material_required",
            retryable=False,
        )
    for value in paths.values():
        path = Path(value)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ProjectionDeliveryError(
                "middleware_mtls_material_invalid",
                retryable=False,
            )
    try:
        context = ssl.create_default_context(cafile=paths["ca"])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            certfile=paths["cert"],
            keyfile=paths["key"],
        )
        return context
    except (OSError, ssl.SSLError):
        raise ProjectionDeliveryError(
            "middleware_mtls_material_invalid",
            retryable=False,
        ) from None


_token_cache: tuple[str, float] | None = None
_token_lock = asyncio.Lock()


async def _middleware_access_token() -> str:
    global _token_cache
    current = time.monotonic()
    if _token_cache is not None and _token_cache[1] > current + 30:
        return _token_cache[0]
    async with _token_lock:
        current = time.monotonic()
        if _token_cache is not None and _token_cache[1] > current + 30:
            return _token_cache[0]
        issuer = os.getenv(
            "CODESTRA_AUTH_ISSUER",
            "https://auth.codestra.co/realms/codestra",
        ).strip().rstrip("/")
        if not _valid_https_url(issuer, origin_only=False):
            raise ProjectionDeliveryError(
                "middleware_oidc_issuer_invalid",
                retryable=False,
            )
        client_id = os.getenv("CODESTRA_KEYCLOAK_CLIENT_ID", "").strip()
        audience = os.getenv("CODESTRA_AUTH_AUDIENCE", "").strip()
        try:
            client_secret = runtime_secret("KLYROW_MIDDLEWARE_CLIENT_SECRET")
        except RuntimeError:
            raise ProjectionDeliveryError(
                "middleware_client_secret_unavailable",
                retryable=False,
            ) from None
        if not client_id or not audience or not client_secret:
            raise ProjectionDeliveryError(
                "middleware_oidc_identity_incomplete",
                retryable=False,
            )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    issuer + "/protocol/openid-connect/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "audience": audience,
                        "scope": (
                            "observability.kpis.write "
                            "observability.incidents.write"
                        ),
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError:
            raise ProjectionDeliveryError(
                "middleware_oidc_unavailable",
                retryable=True,
            ) from None
        if response.status_code != 200 or response.is_redirect:
            raise ProjectionDeliveryError(
                "middleware_oidc_rejected",
                retryable=response.status_code in {408, 425, 429}
                or response.status_code >= 500,
            )
        try:
            document = response.json()
        except ValueError:
            raise ProjectionDeliveryError(
                "middleware_oidc_response_invalid",
                retryable=True,
            ) from None
        token = document.get("access_token") if isinstance(document, dict) else None
        expires_in = document.get("expires_in") if isinstance(document, dict) else None
        if (
            not isinstance(token, str)
            or not token
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 30 <= expires_in <= 900
        ):
            raise ProjectionDeliveryError(
                "middleware_oidc_response_invalid",
                retryable=True,
            )
        _token_cache = (
            token,
            time.monotonic() + min(expires_in, 300),
        )
        return token


def _claim_projection() -> dict[str, Any] | None:
    current = datetime.now(timezone.utc)
    with DB() as session:
        session.execute(
            update(IntegrationOutbox)
            .where(
                IntegrationOutbox.target == MIDDLEWARE_TARGET,
                IntegrationOutbox.event_type.in_(PROJECTION_OPERATIONS),
                IntegrationOutbox.state == "PROCESSING",
                IntegrationOutbox.lease_expires_at < current,
            )
            .values(
                state="RETRY",
                next_attempt_at=current,
                lease_expires_at=None,
                last_error="lease_expired",
                updated_at=current,
            )
        )
        item = session.scalar(
            select(IntegrationOutbox)
            .where(
                IntegrationOutbox.target == MIDDLEWARE_TARGET,
                IntegrationOutbox.event_type.in_(PROJECTION_OPERATIONS),
                IntegrationOutbox.state.in_(("PENDING", "RETRY")),
                IntegrationOutbox.next_attempt_at <= current,
            )
            .order_by(
                IntegrationOutbox.created_at,
                IntegrationOutbox.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            session.commit()
            return None
        item.state = "PROCESSING"
        item.attempts += 1
        item.lease_expires_at = current + timedelta(seconds=60)
        item.updated_at = current
        claim = {
            "id": item.id,
            "tenant_id": item.tenant_id,
            "target": item.target,
            "event_type": item.event_type,
            "payload_json": item.payload_json,
            "idempotency_key": item.idempotency_key,
            "attempt": item.attempts,
        }
        session.commit()
        return claim


async def _send_projection(
    claim: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    event_type = claim["event_type"]
    path = PROJECTION_PATHS.get(event_type)
    if path is None:
        raise ProjectionDeliveryError(
            "middleware_projection_operation_invalid",
            retryable=False,
        )
    try:
        payload = json.loads(claim["payload_json"])
    except (TypeError, ValueError):
        raise ProjectionDeliveryError(
            "middleware_projection_payload_invalid",
            retryable=False,
        ) from None
    if not isinstance(payload, dict):
        raise ProjectionDeliveryError(
            "middleware_projection_payload_invalid",
            retryable=False,
        )
    access_token = token or await _middleware_access_token()
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json",
        "Idempotency-Key": claim["idempotency_key"],
        "X-Correlation-ID": str(payload.get("correlation_id", "")),
        "X-Tenant-ID": claim["tenant_id"],
    }
    owns_client = client is None
    transport = client
    if transport is None:
        transport = httpx.AsyncClient(
            verify=_middleware_tls_context(),
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
    try:
        try:
            response = await transport.post(
                _middleware_base_url() + path,
                headers=headers,
                content=claim["payload_json"].encode("utf-8"),
            )
        except httpx.RequestError:
            raise ProjectionDeliveryError(
                "middleware_projection_transport_error",
                retryable=True,
            ) from None
    finally:
        if owns_client:
            await transport.aclose()
    if response.is_redirect:
        raise ProjectionDeliveryError(
            "middleware_projection_redirect_rejected",
            retryable=False,
        )
    if response.status_code != 202:
        raise ProjectionDeliveryError(
            f"middleware_projection_http_{response.status_code}",
            retryable=response.status_code in {408, 425, 429}
            or response.status_code >= 500,
        )
    content_type = response.headers.get("content-type", "").partition(";")[0]
    if content_type.strip().lower() != "application/json":
        raise ProjectionDeliveryError(
            "middleware_projection_ack_invalid",
            retryable=True,
        )
    try:
        result = response.json()
    except ValueError:
        raise ProjectionDeliveryError(
            "middleware_projection_ack_invalid",
            retryable=True,
        ) from None
    if not (
        isinstance(result, dict)
        and result.get("status") == "accepted"
        and result.get("event_id") == payload.get("event_id")
        and result.get("operation") == event_type
        and result.get("correlation_id") == payload.get("correlation_id")
        and isinstance(result.get("delivery_id"), str)
        and result["delivery_id"]
        and isinstance(result.get("odoo_sync_state"), str)
        and isinstance(result.get("duplicate"), bool)
    ):
        raise ProjectionDeliveryError(
            "middleware_projection_ack_invalid",
            retryable=True,
        )
    return result


def _complete_projection(
    claim: dict[str, Any],
    result: dict[str, Any],
) -> None:
    with DB() as session:
        item = session.scalar(
            select(IntegrationOutbox)
            .where(IntegrationOutbox.id == claim["id"])
            .with_for_update()
        )
        if (
            item is None
            or item.state != "PROCESSING"
            or item.attempts != claim["attempt"]
        ):
            return
        result_key = "middleware-observability:" + result["delivery_id"]
        payload_json = seal_integration_result(
            result,
            tenant_id=item.tenant_id,
            outbox_id=item.id,
            source=item.target,
            result_key=result_key,
        )
        session.add(
            IntegrationResult(
                id=str(uuid.uuid4()),
                tenant_id=item.tenant_id,
                outbox_id=item.id,
                source=item.target,
                result_key=result_key,
                payload_json=payload_json,
            )
        )
        current = datetime.now(timezone.utc)
        item.state = "COMPLETED"
        item.lease_expires_at = None
        item.last_error = None
        item.updated_at = current
        session.commit()


def _fail_projection(
    claim: dict[str, Any],
    error: ProjectionDeliveryError,
) -> None:
    with DB() as session:
        item = session.scalar(
            select(IntegrationOutbox)
            .where(IntegrationOutbox.id == claim["id"])
            .with_for_update()
        )
        if (
            item is None
            or item.state != "PROCESSING"
            or item.attempts != claim["attempt"]
        ):
            return
        retry = error.retryable and item.attempts < MAX_DELIVERY_ATTEMPTS
        current = datetime.now(timezone.utc)
        item.state = "RETRY" if retry else "DEAD_LETTER"
        item.next_attempt_at = current + timedelta(
            seconds=min(300, 2 ** item.attempts)
        )
        item.lease_expires_at = None
        item.last_error = error.code[:255]
        item.updated_at = current
        session.commit()


async def dispatch_observability_outbox(maximum: int = 10) -> int:
    """Relay a bounded batch; disabled means no claim and no external effect."""

    if not _delivery_enabled():
        return 0
    delivered = 0
    for _ in range(max(1, min(maximum, 100))):
        claim = _claim_projection()
        if claim is None:
            break
        try:
            result = await _send_projection(claim)
            _complete_projection(claim, result)
            delivered += 1
        except ProjectionDeliveryError as exc:
            _fail_projection(claim, exc)
            print(
                _canonical_json(
                    {
                        "level": "warning",
                        "system": "klyrow",
                        "event": "observability_projection_delivery_failed",
                        "operation": claim["event_type"],
                        "error": exc.code,
                    }
                )
            )
        except Exception:
            error = ProjectionDeliveryError(
                "observability_projection_internal_error",
                retryable=True,
            )
            _fail_projection(claim, error)
            print(
                _canonical_json(
                    {
                        "level": "error",
                        "system": "klyrow",
                        "event": "observability_projection_delivery_failed",
                        "operation": claim["event_type"],
                        "error": error.code,
                    }
                )
            )
    return delivered


@router.get("/odoo/health")
def odoo_health(
    ctx: dict[str, Any] = Depends(require_observability_read),
    session: Session = Depends(db),
) -> dict[str, Any]:
    summary = _outbox_summary(session, ctx["tenant"])
    dead_letters = summary["counts"].get("DEAD_LETTER", 0)
    configured = _transport_configured()
    enabled = _delivery_enabled()
    status = (
        "disabled"
        if not enabled
        else "healthy"
        if configured and dead_letters == 0
        else "degraded"
    )
    return {
        "status": status,
        "authority": "middleware",
        "target": "odoo",
        "relay": MIDDLEWARE_TARGET,
        "transport": "oidc_service_bearer_plus_private_mtls",
        "configured": configured,
        "enabled": enabled,
        "direct_odoo_database_write": False,
        **summary,
    }


@router.get("/odoo/checkpoints")
def odoo_checkpoints(
    ctx: dict[str, Any] = Depends(require_observability_read),
    session: Session = Depends(db),
) -> dict[str, Any]:
    rows = session.execute(
        select(
            IntegrationOutbox.event_type,
            IntegrationOutbox.state,
            func.count(),
            func.max(IntegrationOutbox.updated_at),
        )
        .where(
            IntegrationOutbox.target == MIDDLEWARE_TARGET,
            IntegrationOutbox.tenant_id == ctx["tenant"],
        )
        .group_by(
            IntegrationOutbox.event_type,
            IntegrationOutbox.state,
        )
        .order_by(
            IntegrationOutbox.event_type,
            IntegrationOutbox.state,
        )
    ).all()
    return {
        "items": [
            {
                "event_type": event_type,
                "state": state,
                "count": int(count),
                "last_updated_at": updated_at.isoformat()
                if updated_at
                else None,
            }
            for event_type, state, count, updated_at in rows
        ],
        "authority": "middleware",
        "direct_odoo_database_write": False,
    }


@router.get("/observability/contract")
def observability_contract(
    ctx: dict[str, Any] = Depends(require_observability_read),
) -> dict[str, Any]:
    del ctx
    return {
        "schema_version": "1.0",
        "service_id": "klyrow-gateway",
        "metrics_path": "/metrics",
        "health_path": "/health/ready",
        "canonical_projection": {
            "api_owner": "appolon1908-hue/Middleware-",
            "business_record_owner": "appolon1908-hue/Odoo",
            "contract": "monitoring/kyyow-observability-sync-v1.json",
            "outbox_target": MIDDLEWARE_TARGET,
            "operations": sorted(PROJECTION_OPERATIONS),
        },
        "signal_owners": {
            "metrics": "Codestra-Prometheus",
            "alerts": "Codestra-Alertmanager",
            "logs": "Codestra-Loki",
            "traces": "Codestra-Tempo",
            "dashboards": "Codestra-Grafana",
            "analytics": "Superset",
            "secrets": "Codestra-OpenBao",
        },
        "integrations": {
            "middleware": "sole_cross_system_write_boundary",
            "odoo": "receives_summaries_through_middleware_only",
        },
        "forbidden_direct_writers": [
            "Codestra-Prometheus",
            "Codestra-Alertmanager",
            "Codestra-Grafana",
            "Codestra-Loki",
            "Codestra-Tempo",
            "Superset",
            "Codestra-OpenBao",
            "klyrow-gateway",
        ],
        "forbidden_metric_labels": sorted(FORBIDDEN_LABELS),
        "direct_odoo_database_write": False,
    }


__all__ = [
    "AlertmanagerAlert",
    "AlertmanagerEnvelope",
    "KpiSnapshotIn",
    "dispatch_observability_outbox",
    "router",
]
