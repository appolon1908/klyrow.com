"""Leased and fenced delivery of immutable Klyrow events to Middleware."""
from __future__ import annotations

import email.utils
import hashlib
import hmac
import json
import os
import random
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import urlsplit

import httpx
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from .business_events import BusinessEventOutbox, EventEnvelope, utcnow
from .main import platform_metric
from .telemetry import traced


PUBLISH_TOTAL = platform_metric(Counter(
    "klyrow_business_event_publish_total",
    "Klyrow business event publication outcomes",
    ["codestra_business", "application", "service", "environment", "server", "region", "deployment", "outcome"],
))
OUTBOX_READY = platform_metric(Gauge(
    "klyrow_business_event_outbox_ready",
    "Klyrow business events ready to publish",
    ["codestra_business", "application", "service", "environment", "server", "region", "deployment"],
))
OUTBOX_DEAD = platform_metric(Gauge(
    "klyrow_business_event_outbox_dead_letter",
    "Klyrow business events in durable dead-letter state",
    ["codestra_business", "application", "service", "environment", "server", "region", "deployment"],
))

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class MiddlewareAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^op_[a-f0-9]{32}$")
    status: Literal["ACCEPTED"]


@dataclass(frozen=True)
class DeliveryResult:
    accepted: bool
    retryable: bool
    error: Optional[str] = None
    retry_after_seconds: Optional[float] = None


@dataclass(frozen=True)
class Lease:
    row_id: str
    event_id: str
    tenant_id: str
    payload: str
    payload_hash: str
    correlation_id: str
    trace_context: str
    attempt_count: int
    lease_owner: str
    lease_expires_at: datetime


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"invalid_{name.lower()}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name.lower()}")
    return value


def _endpoint() -> str:
    endpoint = os.getenv("KLYROW_BUSINESS_EVENTS_URL", "")
    if not endpoint:
        return ""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/v1/events/klyrow"
    ):
        raise ValueError("invalid_business_events_endpoint")
    return endpoint


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=os.getenv("KLYROW_BUSINESS_EVENTS_CA_FILE") or None)
    certificate = os.getenv("KLYROW_BUSINESS_EVENTS_CLIENT_CERT_FILE")
    private_key = os.getenv("KLYROW_BUSINESS_EVENTS_CLIENT_KEY_FILE")
    if bool(certificate) != bool(private_key):
        raise ValueError("business_events_mtls_pair_required")
    if (
        os.getenv("KLYROW_ENV", "development").strip().lower() == "production"
        and not certificate
    ):
        raise ValueError("business_events_production_mtls_required")
    if certificate:
        context.load_cert_chain(certificate, private_key)
    return context


def retry_after_seconds(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), 86_400.0)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return min(max(0.0, (parsed - (now or utcnow())).total_seconds()), 86_400.0)


def backoff_seconds(attempt_count: int, retry_after: Optional[float] = None) -> float:
    base = _integer_setting("KLYROW_BUSINESS_EVENTS_BACKOFF_BASE_SECONDS", 2, 1, 300)
    cap = _integer_setting("KLYROW_BUSINESS_EVENTS_BACKOFF_CAP_SECONDS", 900, base, 86_400)
    exponential = min(float(cap), float(base * (2 ** max(0, attempt_count - 1))))
    jittered = exponential * random.uniform(0.8, 1.2)
    return max(min(float(cap), jittered), retry_after or 0.0)


def operation_id_for_event(event_id: str) -> str:
    digest = hashlib.sha256(f"klyrow\0{event_id}".encode("utf-8")).hexdigest()
    return "op_" + digest[:32]


def classify_response(
    response: httpx.Response,
    *,
    expected_operation_id: str,
    now: Optional[datetime] = None,
) -> DeliveryResult:
    retry_after = retry_after_seconds(response.headers.get("Retry-After"), now)
    if response.status_code in RETRYABLE_STATUSES:
        return DeliveryResult(False, True, f"middleware_http_{response.status_code}", retry_after)
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if response.status_code != 202:
        return DeliveryResult(False, False, f"middleware_http_{response.status_code}")
    if content_type != "application/json":
        return DeliveryResult(False, False, "middleware_202_content_type_invalid")
    try:
        accepted = MiddlewareAccepted.model_validate(response.json())
    except (ValueError, ValidationError):
        return DeliveryResult(False, False, "middleware_202_body_invalid")
    if not hmac.compare_digest(accepted.operation_id, expected_operation_id):
        return DeliveryResult(False, False, "middleware_202_operation_id_mismatch")
    return DeliveryResult(True, False)


def _claim(limit: int, now: datetime, lease_seconds: int, max_attempts: int) -> list[Lease]:
    from .main import DB

    with DB() as session:
        expired = list(session.scalars(select(BusinessEventOutbox).where(
            BusinessEventOutbox.state == "LEASED",
            BusinessEventOutbox.lease_expires_at < now,
        ).order_by(BusinessEventOutbox.created_at).with_for_update(skip_locked=True).limit(limit)))
        for item in expired:
            item.state = "DEAD_LETTER" if item.attempt_count >= max_attempts else "RETRYING"
            item.next_attempt_at = now
            item.lease_owner = None
            item.lease_expires_at = None
            item.last_error = "business_event_lease_expired"
        session.flush()

        candidates = list(session.scalars(select(BusinessEventOutbox).where(
            BusinessEventOutbox.state.in_(("PENDING", "RETRYING")),
            BusinessEventOutbox.next_attempt_at <= now,
        ).order_by(BusinessEventOutbox.created_at, BusinessEventOutbox.id)
          .with_for_update(skip_locked=True).limit(limit)))
        leases: list[Lease] = []
        for item in candidates:
            owner = "bev_" + uuid.uuid4().hex
            expires = now + timedelta(seconds=lease_seconds)
            item.state = "LEASED"
            item.attempt_count += 1
            item.lease_owner = owner
            item.lease_expires_at = expires
            leases.append(Lease(
                row_id=item.id,
                event_id=item.event_id,
                tenant_id=item.tenant_id,
                payload=item.payload,
                payload_hash=item.payload_hash,
                correlation_id=item.correlation_id,
                trace_context=item.trace_context,
                attempt_count=item.attempt_count,
                lease_owner=owner,
                lease_expires_at=expires,
            ))
        session.commit()
    return leases


def _complete(lease: Lease, result: DeliveryResult, max_attempts: int) -> bool:
    """Fence stale workers by owner, attempt and lease expiry before mutation."""
    from .main import DB

    completed_at = utcnow()
    with DB() as session:
        item = session.scalar(select(BusinessEventOutbox).where(
            BusinessEventOutbox.id == lease.row_id,
            BusinessEventOutbox.state == "LEASED",
            BusinessEventOutbox.lease_owner == lease.lease_owner,
            BusinessEventOutbox.attempt_count == lease.attempt_count,
            BusinessEventOutbox.lease_expires_at == lease.lease_expires_at,
            BusinessEventOutbox.lease_expires_at > completed_at,
        ).with_for_update())
        if item is None:
            PUBLISH_TOTAL.labels("fenced").inc()
            return False
        item.lease_owner = None
        item.lease_expires_at = None
        if result.accepted:
            item.state = "DELIVERED"
            item.delivered_at = completed_at
            item.last_error = None
            PUBLISH_TOTAL.labels("delivered").inc()
        elif result.retryable and item.attempt_count < max_attempts:
            item.state = "RETRYING"
            item.next_attempt_at = completed_at + timedelta(
                seconds=backoff_seconds(item.attempt_count, result.retry_after_seconds)
            )
            item.last_error = result.error
            PUBLISH_TOTAL.labels("retrying").inc()
        else:
            item.state = "DEAD_LETTER"
            item.next_attempt_at = completed_at
            item.last_error = result.error or "middleware_delivery_failed"
            PUBLISH_TOTAL.labels("dead_letter").inc()
        session.commit()
    return result.accepted


async def _deliver(
    lease: Lease,
    endpoint: str,
    credential: str,
    hmac_secret: bytes,
    tls: ssl.SSLContext,
    timeout_seconds: int,
) -> DeliveryResult:
    if hashlib.sha256(lease.payload.encode("utf-8")).hexdigest() != lease.payload_hash:
        return DeliveryResult(False, False, "business_event_payload_integrity_failure")
    try:
        event = EventEnvelope.model_validate_json(lease.payload)
        if event.id != lease.event_id or event.tenant_id != lease.tenant_id:
            return DeliveryResult(False, False, "business_event_identity_mismatch")
        stored_trace = json.loads(lease.trace_context)
        if not isinstance(stored_trace, dict):
            raise ValueError("trace_context_not_object")
    except (ValueError, ValidationError, json.JSONDecodeError):
        return DeliveryResult(False, False, "business_event_payload_invalid")
    body = lease.payload.encode("utf-8")
    timestamp = str(int(utcnow().timestamp()))
    canonical = f"{timestamp}\n{event.id}\nklyrow\n".encode("utf-8") + body
    signature = hmac.new(hmac_secret, canonical, hashlib.sha256).hexdigest()
    headers = {
        "Authorization": "Bearer " + credential,
        "X-Event-Id": event.id,
        "X-Timestamp": timestamp,
        "X-Signature": "sha256=" + signature,
        "X-Correlation-Id": event.correlation_id,
        "Idempotency-Key": event.id,
    }
    headers.update({
        key: value for key, value in stored_trace.items()
        if key.lower() in {"traceparent", "tracestate"} and isinstance(value, str)
    })
    try:
        with traced("business event publish", stored_trace):
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                verify=tls,
            ) as client:
                response = await client.post(endpoint, content=body, headers={
                    **headers,
                    "Content-Type": "application/json",
                })
        return classify_response(
            response,
            expected_operation_id=operation_id_for_event(event.id),
        )
    except httpx.TransportError:
        return DeliveryResult(False, True, "middleware_network_error")


def refresh_metrics() -> None:
    from .main import DB

    with DB() as session:
        ready = session.scalar(select(func.count()).select_from(BusinessEventOutbox).where(
            BusinessEventOutbox.state.in_(("PENDING", "RETRYING"))
        ))
        dead = session.scalar(select(func.count()).select_from(BusinessEventOutbox).where(
            BusinessEventOutbox.state == "DEAD_LETTER"
        ))
    OUTBOX_READY.set(int(ready or 0))
    OUTBOX_DEAD.set(int(dead or 0))


async def dispatch(limit: int = 50) -> int:
    from .main import runtime_secret

    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("invalid_dispatch_limit")
    if os.getenv("KLYROW_BUSINESS_EVENTS_ENABLED", "false").strip().lower() != "true":
        return 0
    endpoint = _endpoint()
    if not endpoint:
        raise ValueError("business_events_endpoint_required")
    credential = runtime_secret("KLYROW_BUSINESS_EVENTS_TOKEN")
    hmac_secret = runtime_secret("KLYROW_BUSINESS_EVENTS_HMAC_SECRET").encode("utf-8")
    if not credential or len(hmac_secret) < 32:
        raise ValueError("business_events_credentials_invalid")
    max_attempts = _integer_setting("KLYROW_BUSINESS_EVENTS_MAX_ATTEMPTS", 8, 1, 100)
    lease_seconds = _integer_setting("KLYROW_BUSINESS_EVENTS_LEASE_SECONDS", 60, 5, 3600)
    timeout_seconds = _integer_setting("KLYROW_BUSINESS_EVENTS_TIMEOUT_SECONDS", 5, 1, 120)
    tls = _tls_context()
    leases = _claim(limit, utcnow(), lease_seconds, max_attempts)
    delivered = 0
    for lease in leases:
        result = await _deliver(
            lease,
            endpoint,
            credential,
            hmac_secret,
            tls,
            timeout_seconds,
        )
        delivered += int(_complete(lease, result, max_attempts))
    refresh_metrics()
    return delivered
