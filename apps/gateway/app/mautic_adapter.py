"""Bounded asynchronous Mautic adapter backed by the durable integration outbox."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import DB, Base, runtime_secret
from .durable_keys import KeyringError, load_keyring
from .durable_results import MAX_RESULT_BYTES, seal_integration_result
from .mautic_contract import SUPPORTED_MAUTIC_COMMANDS
from .operations import IntegrationOutbox, IntegrationResult

now = lambda: datetime.now(UTC)
MAX_ATTEMPTS = 5
LEASE_SECONDS = 30

MAUTIC_SYNC_ROUTES: dict[str, tuple[str, str | None]] = {
    "contacts": ("/api/contacts", "/api/contacts/{id}"),
    "companies": ("/api/companies", "/api/companies/{id}"),
    "segments": ("/api/segments", "/api/segments/{id}"),
    "campaigns": ("/api/campaigns", "/api/campaigns/{id}"),
    "campaign_events": ("/api/campaigns/events", "/api/campaigns/events/{id}"),
    "emails": ("/api/emails", "/api/emails/{id}"),
    "forms": ("/api/forms", "/api/forms/{id}"),
    "hooks": ("/api/hooks", "/api/hooks/{id}"),
    "hook_triggers": ("/api/hooks/triggers", None),
    "tags": ("/api/tags", "/api/tags/{id}"),
    "reports": ("/api/reports", "/api/reports/{id}"),
    "users_self": ("/api/users/self", None),
}

MAUTIC_ORIGIN_HEADERS = {
    "Host": "app.klyrow.com",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Prefix": "/mautic",
}


class MauticAdapterState(Base):
    __tablename__ = "mautic_adapter_state"

    state_key: Mapped[str] = mapped_column(String, primary_key=True)
    failure_streak: Mapped[int] = mapped_column(Integer, default=0)
    circuit_open_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def _bounded_id(value: Any, field: str) -> str:
    if not isinstance(value, (str, int)):
        raise TypeError(field + "_required")
    result = str(value).strip()
    if (
        not result
        or len(result) > 160
        or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for ch in result
        )
    ):
        raise ValueError(field + "_invalid")
    return quote(result, safe="")


def mautic_request(command: str, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    """Translate the governed command vocabulary into Mautic's canonical API."""

    if command not in SUPPORTED_MAUTIC_COMMANDS:
        raise ValueError("mautic_command_unsupported")

    provider_id = payload.get("provider_id")
    if command.startswith("contact."):
        resource = "contacts"
    elif command.startswith("segment."):
        resource = "segments"
    elif command.startswith("campaign."):
        resource = "campaigns"
    else:
        resource = ""

    if command.endswith(".upsert.v1") and resource:
        path = f"/api/{resource}/new"
        if provider_id is not None:
            path = f"/api/{resource}/{_bounded_id(provider_id, 'provider_id')}/edit"
        body = {key: value for key, value in payload.items() if key != "provider_id"}
        return ("PATCH" if provider_id is not None else "POST", path, body)
    if command.endswith(".delete.v1") and resource:
        return (
            "DELETE",
            f"/api/{resource}/{_bounded_id(provider_id, 'provider_id')}/delete",
            None,
        )
    if command in {"campaign.publish.v1", "campaign.pause.v1"}:
        return (
            "PATCH",
            "/api/campaigns/" + _bounded_id(provider_id, "provider_id") + "/edit",
            {"isPublished": command == "campaign.publish.v1"},
        )
    if command in {"campaign_membership.add.v1", "campaign_membership.remove.v1"}:
        campaign_id = _bounded_id(payload.get("campaign_id"), "campaign_id")
        contact_id = _bounded_id(payload.get("contact_id"), "contact_id")
        action = "add" if command.endswith("add.v1") else "remove"
        return "POST", f"/api/campaigns/{campaign_id}/contact/{contact_id}/{action}", {}
    if command in {"segment_membership.add.v1", "segment_membership.remove.v1"}:
        segment_id = _bounded_id(payload.get("segment_id"), "segment_id")
        contact_id = _bounded_id(payload.get("contact_id"), "contact_id")
        action = "add" if command.endswith("add.v1") else "remove"
        return "POST", f"/api/segments/{segment_id}/contact/{contact_id}/{action}", {}
    if command == "email_campaign.state.v1":
        email_id = _bounded_id(payload.get("email_id"), "email_id")
        return (
            "PATCH",
            f"/api/emails/{email_id}/edit",
            {"isPublished": bool(payload.get("published"))},
        )
    if command == "webhook.register.v1":
        return "POST", "/api/hooks/new", payload
    if command == "sync.request.v1":
        resource_name = payload.get("resource")
        route = MAUTIC_SYNC_ROUTES.get(resource_name) if isinstance(resource_name, str) else None
        if route is None:
            raise ValueError("sync_resource_invalid")
        path, item_route = route
        if provider_id is not None:
            if item_route is None:
                raise ValueError("sync_resource_item_unsupported")
            path = item_route.format(id=_bounded_id(provider_id, "provider_id"))
        return "GET", path, None
    if command == "form_submissions.read.v1":
        form_id = _bounded_id(payload.get("form_id"), "form_id")
        submission_id = payload.get("submission_id")
        path = f"/api/forms/{form_id}/submissions"
        if submission_id is not None:
            path += "/" + _bounded_id(submission_id, "submission_id")
        return "GET", path, None
    raise ValueError("mautic_command_unsupported")


def _configuration() -> tuple[str, str, str]:
    base_url = os.getenv("KLYROW_MAUTIC_API_URL", "http://mautic").rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "mautic"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("KLYROW_MAUTIC_API_URL must be the private http://mautic endpoint")
    client_id = runtime_secret("KLYROW_MAUTIC_API_CLIENT_ID")
    client_secret = runtime_secret("KLYROW_MAUTIC_API_CLIENT_SECRET")
    if len(client_id.encode()) < 16:
        raise RuntimeError("KLYROW_MAUTIC_API_CLIENT_ID must contain at least 16 bytes")
    if len(client_secret.encode()) < 32:
        raise RuntimeError("KLYROW_MAUTIC_API_CLIENT_SECRET must contain at least 32 bytes")
    return base_url, client_id, client_secret


async def _oauth_access_token(client: httpx.AsyncClient, client_id: str, client_secret: str) -> str:
    response = await client.post(
        "/oauth/v2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Accept": "application/json"},
    )
    if len(response.content) > 65536:
        raise RuntimeError("mautic_oauth_response_too_large")
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    if not isinstance(token, str) or len(token.encode()) < 32:
        raise RuntimeError("mautic_oauth_access_token_invalid")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise RuntimeError("mautic_oauth_token_type_invalid")
    if not isinstance(expires_in, int) or expires_in <= 0:
        raise RuntimeError("mautic_oauth_expiry_invalid")
    return token


def _recover_expired(session: Session, current: datetime) -> None:
    rows = session.scalars(
        select(IntegrationOutbox)
        .where(
            IntegrationOutbox.target == "MAUTIC",
            IntegrationOutbox.state == "PROCESSING",
            IntegrationOutbox.lease_expires_at < current,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for item in rows:
        item.state = "DEAD_LETTER"
        item.last_error = "worker_lease_expired_ambiguous"
        item.lease_expires_at = None
        item.updated_at = current
    session.commit()


def _claim(session: Session) -> IntegrationOutbox | None:
    current = now()
    _recover_expired(session, current)
    circuit = session.get(MauticAdapterState, "primary")
    if circuit and circuit.circuit_open_until and circuit.circuit_open_until > current:
        return None
    item = session.scalar(
        select(IntegrationOutbox)
        .where(
            IntegrationOutbox.target == "MAUTIC",
            IntegrationOutbox.state.in_(("PENDING", "RETRY")),
            IntegrationOutbox.next_attempt_at <= current,
            ~select(IntegrationResult.id)
            .where(
                IntegrationResult.outbox_id == IntegrationOutbox.id,
                IntegrationResult.tenant_id == IntegrationOutbox.tenant_id,
                IntegrationResult.source == "MAUTIC_LATE",
            )
            .exists(),
        )
        .order_by(IntegrationOutbox.created_at)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
        .limit(1)
    )
    if item:
        item.state = "PROCESSING"
        item.attempts += 1
        item.lease_expires_at = current + timedelta(seconds=LEASE_SECONDS)
        item.updated_at = current
        session.commit()
    return item


def _has_late_observation(session: Session, item: IntegrationOutbox) -> bool:
    return (
        session.scalar(
            select(IntegrationResult.id)
            .where(
                IntegrationResult.outbox_id == item.id,
                IntegrationResult.tenant_id == item.tenant_id,
                IntegrationResult.source == "MAUTIC_LATE",
            )
            .limit(1)
        )
        is not None
    )


def _current_claim(
    item: IntegrationOutbox | None, expected_attempt: int, current: datetime
) -> bool:
    if item is None or item.state != "PROCESSING" or item.attempts != expected_attempt:
        return False
    deadline = item.lease_expires_at
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline > current


def _failure(
    session: Session,
    item_id: str,
    error: str,
    *,
    retryable: bool,
    expected_attempt: int,
    affects_circuit: bool = True,
) -> bool:
    current = now()
    item = session.scalar(
        select(IntegrationOutbox)
        .where(IntegrationOutbox.id == item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # A delayed failure cannot rewrite a cancellation/new claim or poison its circuit.
    if not _current_claim(item, expected_attempt, current):
        return False
    if affects_circuit:
        circuit = session.get(MauticAdapterState, "primary") or MauticAdapterState(
            state_key="primary"
        )
        circuit.failure_streak = (circuit.failure_streak or 0) + 1
        circuit.last_failure_at = current
        circuit.updated_at = current
        if circuit.failure_streak >= 5:
            circuit.circuit_open_until = current + timedelta(seconds=60)
        session.add(circuit)
    can_retry = (
        retryable and item.attempts < MAX_ATTEMPTS and not _has_late_observation(session, item)
    )
    item.state = "RETRY" if can_retry else "DEAD_LETTER"
    item.next_attempt_at = current + timedelta(seconds=min(300, 2**item.attempts))
    item.lease_expires_at = None
    item.last_error = error[:500]
    item.updated_at = current
    session.commit()
    return True


def _record_late_completion(
    session: Session, item: IntegrationOutbox, expected_attempt: int
) -> None:
    # Preserve terminal/newer state; retain a separate, non-success observation.
    source = "MAUTIC_LATE"
    key = f"mautic:late:{item.id}:{expected_attempt}"
    prior = session.scalar(
        select(IntegrationResult).where(
            IntegrationResult.tenant_id == item.tenant_id,
            IntegrationResult.source == source,
            IntegrationResult.result_key == key,
        )
    )
    if prior is None:
        session.add(
            IntegrationResult(
                id=__import__("uuid").uuid4().hex,
                tenant_id=item.tenant_id,
                outbox_id=item.id,
                source=source,
                result_key=key,
                payload_json=seal_integration_result(
                    {
                        "status": "RECONCILIATION_REQUIRED",
                        "attempt": expected_attempt,
                        "reason": "provider_completed_after_operation_state_changed",
                    },
                    tenant_id=item.tenant_id,
                    outbox_id=item.id,
                    source=source,
                    result_key=key,
                ),
            )
        )
    session.commit()


def _success(
    session: Session, item_id: str, response: dict[str, Any], *, expected_attempt: int
) -> bool:
    current = now()
    item = session.scalar(
        select(IntegrationOutbox)
        .where(IntegrationOutbox.id == item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        return False
    if item.state == "COMPLETED" and item.attempts == expected_attempt:
        return False
    if not _current_claim(item, expected_attempt, current):
        _record_late_completion(session, item, expected_attempt)
        return False
    result_key = "mautic:" + item.id
    if not session.scalar(
        select(IntegrationResult).where(
            IntegrationResult.tenant_id == item.tenant_id,
            IntegrationResult.source == "MAUTIC",
            IntegrationResult.result_key == result_key,
        )
    ):
        session.add(
            IntegrationResult(
                id=item.id,
                tenant_id=item.tenant_id,
                outbox_id=item.id,
                source="MAUTIC",
                result_key=result_key,
                payload_json=seal_integration_result(
                    response,
                    tenant_id=item.tenant_id,
                    outbox_id=item.id,
                    source="MAUTIC",
                    result_key=result_key,
                ),
            )
        )
    item.state = "COMPLETED"
    item.lease_expires_at = None
    item.last_error = None
    item.updated_at = current
    circuit = session.get(MauticAdapterState, "primary") or MauticAdapterState(state_key="primary")
    circuit.failure_streak = 0
    circuit.circuit_open_until = None
    circuit.last_success_at = current
    circuit.updated_at = current
    session.add(circuit)
    session.commit()
    return True


async def dispatch_mautic_outbox(limit: int = 20) -> dict[str, int]:
    completed = failed = 0
    try:
        base_url, client_id, client_secret = _configuration()
        load_keyring()
    except (RuntimeError, KeyringError):
        return {"completed": 0, "failed": 0, "disabled": 1}
    timeout = httpx.Timeout(10.0, connect=3.0)
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        headers=MAUTIC_ORIGIN_HEADERS,
    ) as client:
        try:
            token = await _oauth_access_token(client, client_id, client_secret)
        except (
            httpx.HTTPError,
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
            json.JSONDecodeError,
        ):
            return {"completed": 0, "failed": 0, "disabled": 0, "auth_failed": 1}
        for _ in range(limit):
            with DB() as session:
                item = _claim(session)
                if not item:
                    break
                snapshot = (
                    item.id,
                    item.event_type,
                    json.loads(item.payload_json),
                    item.idempotency_key,
                    item.attempts,
                )
            item_id, command, stored, idempotency_key, expected_attempt = snapshot
            with DB() as session:
                current = session.get(IntegrationOutbox, item_id)
                if not _current_claim(current, expected_attempt, now()) or _has_late_observation(
                    session, current
                ):
                    continue
            provider_called = False
            try:
                method, path, body = mautic_request(command, stored.get("payload") or {})
                provider_called = True
                response = await client.request(
                    method,
                    path,
                    json=body,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Idempotency-Key": idempotency_key,
                        "X-Correlation-ID": stored["envelope"]["correlation_id"],
                        "Accept": "application/json",
                    },
                )
                if response.status_code == 429:
                    raise httpx.HTTPStatusError(
                        "mautic_rate_limited",
                        request=response.request,
                        response=response,
                    )
                if response.status_code >= 500:
                    with DB() as session:
                        _failure(
                            session,
                            item_id,
                            "mautic_ambiguous_http_" + str(response.status_code),
                            retryable=False,
                            expected_attempt=expected_attempt,
                        )
                    failed += 1
                    continue
                response.raise_for_status()
                if len(response.content) > MAX_RESULT_BYTES:
                    raise RuntimeError("mautic_response_too_large")
                result = (
                    response.json() if response.content else {"status_code": response.status_code}
                )
                with DB() as session:
                    accepted = _success(session, item_id, result, expected_attempt=expected_attempt)
                completed += int(accepted)
            except httpx.HTTPStatusError as error:
                retryable = error.response.status_code == 429
                with DB() as session:
                    _failure(
                        session,
                        item_id,
                        "mautic_http_" + str(error.response.status_code),
                        retryable=retryable,
                        expected_attempt=expected_attempt,
                        affects_circuit=error.response.status_code in {401, 403, 408, 429},
                    )
                failed += 1
            except httpx.ConnectError:
                with DB() as session:
                    _failure(
                        session,
                        item_id,
                        "mautic_connect_error",
                        retryable=True,
                        expected_attempt=expected_attempt,
                    )
                failed += 1
            except (httpx.TimeoutException, httpx.TransportError):
                with DB() as session:
                    _failure(
                        session,
                        item_id,
                        "mautic_transport_ambiguous",
                        retryable=False,
                        expected_attempt=expected_attempt,
                    )
                failed += 1
            except (
                KeyError,
                TypeError,
                ValueError,
                RuntimeError,
                json.JSONDecodeError,
                HTTPException,
            ) as error:
                with DB() as session:
                    _failure(
                        session,
                        item_id,
                        type(error).__name__,
                        retryable=False,
                        expected_attempt=expected_attempt,
                        affects_circuit=provider_called,
                    )
                failed += 1
    return {"completed": completed, "failed": failed, "disabled": 0}


async def mautic_worker_loop() -> None:
    while True:
        await dispatch_mautic_outbox()
        await asyncio.sleep(2)
