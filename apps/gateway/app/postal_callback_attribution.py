"""Signed Postal lifecycle callback attribution for multi-tenant delivery."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .main import EmailOutbox, Message, PostalEvent, Tenant, db
from . import main as core
from .postal_provisioning import READY, PostalTenantMapping
from .provider import ProviderMessage

router = APIRouter(tags=["Postal callbacks"])
_INSTALLED = False
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PostalAttribution:
    tenant_id: str
    local_message_id: str | None
    source: str


def tenant_callback_attribution_required() -> bool:
    return (
        os.getenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "false").strip().lower()
        in _TRUE_VALUES
    )


def install_postal_callback_extension() -> None:
    """Remove the historical global-tenant callback before route registration."""

    global _INSTALLED
    if _INSTALLED:
        return
    for route in list(core.app.router.routes):
        if getattr(route, "path", "") == "/v1/webhooks/postal-native" and "POST" in getattr(
            route, "methods", set()
        ):
            core.app.router.routes.remove(route)
    _INSTALLED = True


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def _server_references(*sources: dict[str, Any]) -> set[str]:
    references: set[str] = set()
    for source in sources:
        for key in (
            "server_id",
            "server_uuid",
            "server_permalink",
            "postal_server_id",
        ):
            value = _text(source.get(key))
            if value:
                references.add(value)
        nested = source.get("server")
        if isinstance(nested, dict):
            for key in ("id", "uuid", "permalink"):
                value = _text(nested.get(key))
                if value:
                    references.add(value)
        else:
            value = _text(nested)
            if value:
                references.add(value)
    return references


def _resolve_attribution(
    session: Session,
    *,
    event_id: str,
    raw: dict[str, Any],
    payload: dict[str, Any],
    message: dict[str, Any],
) -> PostalAttribution:
    existing = session.get(PostalEvent, event_id)
    if existing:
        return PostalAttribution(
            tenant_id=existing.tenant_id,
            local_message_id=existing.message_id or None,
            source="existing_event",
        )

    tag = _text(message.get("tag"))
    provider_references = {
        value
        for value in (
            _text(message.get("message_id")),
            _text(message.get("id")),
            _text(payload.get("provider_message_id")),
            _text(payload.get("message_id")),
        )
        if value
    }
    general_references = set(provider_references)
    for value in (
        tag,
        _text(payload.get("correlation_id")),
        _text(raw.get("correlation_id")),
    ):
        if value:
            general_references.add(value)

    candidates: list[PostalAttribution] = []

    # Postal echoes the opaque local message ID in `tag`. Prefer this exact,
    # application-generated correlation before provider-assigned identifiers.
    if tag:
        for row in session.scalars(select(EmailOutbox).where(EmailOutbox.message_id == tag)).all():
            candidates.append(PostalAttribution(row.tenant_id, row.message_id, "outbox_tag"))
        local_message = session.get(Message, tag)
        if local_message:
            candidates.append(
                PostalAttribution(local_message.tenant_id, local_message.id, "message_tag")
            )
        for row in session.scalars(
            select(ProviderMessage).where(
                or_(
                    ProviderMessage.id == tag,
                    ProviderMessage.correlation_id == tag,
                )
            )
        ).all():
            candidates.append(PostalAttribution(row.tenant_id, row.id, "provider_tag"))

    if provider_references:
        for row in session.scalars(
            select(EmailOutbox).where(EmailOutbox.provider_message_id.in_(provider_references))
        ).all():
            candidates.append(
                PostalAttribution(row.tenant_id, row.message_id, "outbox_provider_message_id")
            )
        for row in session.scalars(
            select(ProviderMessage).where(
                ProviderMessage.provider_message_id.in_(provider_references)
            )
        ).all():
            candidates.append(PostalAttribution(row.tenant_id, row.id, "provider_message_id"))

    if general_references:
        for row in session.scalars(
            select(EmailOutbox).where(EmailOutbox.message_id.in_(general_references))
        ).all():
            candidates.append(PostalAttribution(row.tenant_id, row.message_id, "outbox_reference"))
        for row in session.scalars(
            select(ProviderMessage).where(
                or_(
                    ProviderMessage.id.in_(general_references),
                    ProviderMessage.correlation_id.in_(general_references),
                )
            )
        ).all():
            candidates.append(PostalAttribution(row.tenant_id, row.id, "provider_reference"))

    server_references = _server_references(raw, payload, message)
    if server_references:
        for mapping in session.scalars(
            select(PostalTenantMapping).where(
                PostalTenantMapping.state == READY,
                or_(
                    PostalTenantMapping.provider_server_id.in_(server_references),
                    PostalTenantMapping.provider_server_permalink.in_(server_references),
                ),
            )
        ).all():
            candidates.append(PostalAttribution(mapping.tenant_id, None, "postal_server_mapping"))

    tenants = {candidate.tenant_id for candidate in candidates}
    if len(tenants) > 1:
        raise HTTPException(503, "postal_tenant_attribution_ambiguous")
    if len(tenants) == 1:
        tenant_id = next(iter(tenants))
        local_message_id = next(
            (
                candidate.local_message_id
                for candidate in candidates
                if candidate.tenant_id == tenant_id and candidate.local_message_id
            ),
            None,
        )
        source = next(
            candidate.source for candidate in candidates if candidate.tenant_id == tenant_id
        )
        return PostalAttribution(tenant_id, local_message_id, source)

    if tenant_callback_attribution_required():
        raise HTTPException(503, "postal_tenant_attribution_unavailable")

    tenant_id = os.getenv("KLYROW_POSTAL_TENANT_ID", "").strip()
    if not tenant_id:
        raise HTTPException(503, "postal_tenant_not_configured")
    if session.get(Tenant, tenant_id) is None:
        raise HTTPException(503, "postal_tenant_not_configured")
    return PostalAttribution(tenant_id, None, "legacy_single_tenant")


def _recipient(message: dict[str, Any]) -> str:
    value = message.get("to")
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    return _text(value)


@router.post("/v1/webhooks/postal-native", status_code=202)
async def postal_native_hook(
    request: Request,
    x_postal_signature_256: str = Header(default=""),
    session: Session = Depends(db),
):
    body = await request.body()
    core.verify_postal_signature(body, x_postal_signature_256)
    try:
        raw = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "invalid_json") from exc
    if not isinstance(raw, dict):
        raise HTTPException(400, "invalid_json")

    event_id = _text(raw.get("uuid"))
    if not event_id:
        raise HTTPException(400, "event_id_required")
    try:
        event_timestamp = float(raw.get("timestamp"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(401, "invalid_postal_timestamp") from exc
    if event_timestamp > time.time() + 300 or event_timestamp < time.time() - 86400:
        raise HTTPException(401, "expired_postal_event")

    event_name = _text(raw.get("event"))
    canonical = core.POSTAL_EVENTS.get(event_name)
    if not canonical:
        raise HTTPException(422, "unsupported_postal_event")
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        raise HTTPException(422, "invalid_postal_payload")
    message = payload.get("message") or payload.get("original_message") or {}
    if not isinstance(message, dict):
        raise HTTPException(422, "invalid_postal_payload")

    attribution = _resolve_attribution(
        session,
        event_id=event_id,
        raw=raw,
        payload=payload,
        message=message,
    )
    existing = session.get(PostalEvent, event_id)
    if existing and existing.state == "delivered":
        return {"accepted": True, "duplicate": True}

    provider_message_id = _text(message.get("message_id")) or _text(message.get("id"))
    correlation = (
        _text(payload.get("correlation_id"))
        or _text(message.get("tag"))
        or provider_message_id
        or event_id
    )
    local_message_id = (
        attribution.local_message_id
        or _text(message.get("id"))
        or provider_message_id
        or correlation
    )
    outbox = session.scalar(
        select(EmailOutbox).where(
            EmailOutbox.tenant_id == attribution.tenant_id,
            EmailOutbox.message_id == local_message_id,
        )
    )
    if outbox and outbox.correlation_id:
        correlation = outbox.correlation_id
    operation_id = outbox.operation_id if outbox and outbox.operation_id else local_message_id
    recipient = _recipient(message)
    canonical_status = core.TERMINAL_MESSAGE_STATUSES.get(canonical, canonical.rsplit(".", 1)[-1])
    normalized = {
        "event": canonical,
        "event_id": event_id,
        "schema_version": "1.0",
        "event_version": "1.0",
        "tenant_id": attribution.tenant_id,
        "operation_id": operation_id,
        "payload_hash": hashlib.sha256(body).hexdigest(),
        "message_id": local_message_id,
        "provider_message_id": provider_message_id or local_message_id,
        "stream": "transactional",
        "correlation_id": correlation,
        "causation_id": correlation,
        "recipient_reference": "sha256:" + hashlib.sha256(recipient.lower().encode()).hexdigest(),
        "recipient": recipient,
        "sender": message.get("from"),
        "provider": "postal",
        "status": payload.get("status") or canonical.rsplit(".", 1)[-1],
        "canonical_status": canonical_status,
        "occurred_at": datetime.fromtimestamp(event_timestamp, UTC).isoformat(),
        "attempt": 1,
        "metadata": {
            "provider_event": event_name,
            "provider_message_token": message.get("token"),
            "tenant_attribution": attribution.source,
        },
    }

    item = existing
    if not item:
        item = PostalEvent(
            id=event_id,
            event_type=canonical,
            correlation_id=correlation,
            message_id=local_message_id,
            tenant_id=attribution.tenant_id,
            payload=json.dumps(normalized, separators=(",", ":"), sort_keys=True),
        )
        session.add(item)
    item.attempts = (item.attempts or 0) + 1
    item.updated_at = datetime.now(UTC)
    session.commit()

    core.persist_email_event(
        session,
        event_id=event_id,
        tenant_id=attribution.tenant_id,
        message_id=local_message_id,
        correlation_id=correlation,
        event_type=canonical,
        recipient=normalized.get("recipient"),
        raw_status=normalized.get("status"),
        payload=item.payload,
    )
    item = session.get(PostalEvent, event_id)
    item.payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    session.commit()

    delivered = await core.emit_middleware(
        "klyrow." + canonical,
        {**normalized, "customer_id": attribution.tenant_id},
    )
    item = session.get(PostalEvent, event_id)
    if delivered:
        item.state = "delivered"
        item.last_error = None
        session.commit()
        core.MAIL.labels(canonical.rsplit(".", 1)[-1]).inc()
        return {"accepted": True}
    item.state = "dlq" if item.attempts >= 5 else "retry"
    item.last_error = "middleware_delivery_failed"
    session.commit()
    raise HTTPException(503, "middleware_delivery_pending")
