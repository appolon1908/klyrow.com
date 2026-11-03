"""Tenant-safe Postal native webhook routing for the production Klyrow app."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .main import EmailOutbox, Message, PostalEvent, Tenant, db
from . import main
from .postal_provisioning import READY, PostalTenantMapping
from .provider import ProviderMessage

router = APIRouter(tags=["Postal webhooks"])
_INSTALLED = False


def tenant_postal_provisioning_enabled() -> bool:
    return os.getenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def install_postal_webhook_extension() -> None:
    """Remove the global-tenant handler before the replacement route is added."""

    global _INSTALLED
    if _INSTALLED:
        return
    for route in list(main.app.router.routes):
        if getattr(route, "path", "") == "/v1/webhooks/postal-native" and "POST" in getattr(
            route, "methods", set()
        ):
            main.app.router.routes.remove(route)
    _INSTALLED = True


def _one_tenant(
    session: Session,
    tenant_ids: Iterable[str | None],
) -> str:
    candidates = {value for value in tenant_ids if value}
    if len(candidates) > 1:
        raise HTTPException(409, "postal_tenant_resolution_conflict")
    if len(candidates) == 1:
        tenant_id = next(iter(candidates))
        tenant = session.get(Tenant, tenant_id)
        if not tenant or not tenant.enabled:
            raise HTTPException(422, "postal_tenant_unavailable")
        if tenant_postal_provisioning_enabled():
            mapping = session.get(PostalTenantMapping, tenant_id, populate_existing=True)
            if not mapping or mapping.state != READY:
                raise HTTPException(422, "postal_tenant_mapping_not_ready")
        return tenant_id

    # Preserve the released single-tenant callback contract only while the
    # tenant-provisioning delivery mode is disabled. The multi-tenant mode is
    # always fail-closed and must resolve a local message/provider mapping.
    if not tenant_postal_provisioning_enabled():
        legacy_tenant = os.getenv("KLYROW_POSTAL_TENANT_ID", "").strip()
        tenant = session.get(Tenant, legacy_tenant) if legacy_tenant else None
        if tenant and tenant.enabled:
            return legacy_tenant
    raise HTTPException(422, "postal_tenant_unresolved")


def resolve_postal_tenant(
    session: Session,
    *,
    provider_message_id: str = "",
    correlation_id: str = "",
    local_message_id: str = "",
) -> str:
    """Resolve one tenant from durable local send records, never callback input."""

    candidates: list[str | None] = []
    provider_message_id = provider_message_id.strip()
    correlation_id = correlation_id.strip()
    local_message_id = local_message_id.strip()

    outbox_filters = []
    if provider_message_id:
        outbox_filters.append(EmailOutbox.provider_message_id == provider_message_id)
    if correlation_id:
        outbox_filters.extend(
            [
                EmailOutbox.message_id == correlation_id,
                EmailOutbox.provider_message_id == correlation_id,
            ]
        )
    if local_message_id:
        outbox_filters.append(EmailOutbox.message_id == local_message_id)
    if outbox_filters:
        candidates.extend(
            session.scalars(select(EmailOutbox.tenant_id).where(or_(*outbox_filters))).all()
        )

    message_ids = {value for value in (correlation_id, local_message_id) if value}
    if message_ids:
        candidates.extend(
            session.scalars(select(Message.tenant_id).where(Message.id.in_(message_ids))).all()
        )

    provider_filters = []
    if provider_message_id:
        provider_filters.append(ProviderMessage.provider_message_id == provider_message_id)
    if correlation_id:
        provider_filters.extend(
            [
                ProviderMessage.correlation_id == correlation_id,
                ProviderMessage.id == correlation_id,
            ]
        )
    if local_message_id:
        provider_filters.append(ProviderMessage.id == local_message_id)
    if provider_filters:
        candidates.extend(
            session.scalars(select(ProviderMessage.tenant_id).where(or_(*provider_filters))).all()
        )

    return _one_tenant(session, candidates)


def _recipient(message: dict) -> str:
    value = message.get("to")
    if isinstance(value, list):
        return str(value[0] if value else "")
    return str(value or "")


@router.post("/v1/webhooks/postal-native", status_code=202)
async def postal_native_hook(
    request: Request,
    x_postal_signature_256: str = Header(default=""),
    session: Session = Depends(db),
):
    body = await request.body()
    main.verify_postal_signature(body, x_postal_signature_256)
    try:
        raw = json.loads(body)
    except ValueError:
        raise HTTPException(400, "invalid_json")

    event_id = str(raw.get("uuid") or "")
    if not event_id:
        raise HTTPException(400, "event_id_required")
    try:
        event_timestamp = float(raw.get("timestamp"))
    except (TypeError, ValueError):
        raise HTTPException(401, "invalid_postal_timestamp")
    if event_timestamp > time.time() + 300 or event_timestamp < time.time() - 86400:
        raise HTTPException(401, "expired_postal_event")

    event_name = str(raw.get("event") or "")
    canonical = main.POSTAL_EVENTS.get(event_name)
    if not canonical:
        raise HTTPException(422, "unsupported_postal_event")
    payload = raw.get("payload") or {}
    message = payload.get("message") or payload.get("original_message") or {}
    if not isinstance(message, dict):
        raise HTTPException(400, "invalid_postal_message")

    provider_message_id = str(message.get("message_id") or message.get("id") or "")
    correlation = str(
        payload.get("correlation_id") or message.get("tag") or provider_message_id or event_id
    )
    local_message_id = str(message.get("id") or correlation)
    tenant_id = resolve_postal_tenant(
        session,
        provider_message_id=provider_message_id,
        correlation_id=correlation,
        local_message_id=local_message_id,
    )

    recipient = _recipient(message)
    canonical_status = main.TERMINAL_MESSAGE_STATUSES.get(canonical, canonical.rsplit(".", 1)[-1])
    normalized = {
        "event": canonical,
        "event_id": event_id,
        "event_version": "1.0",
        "tenant_id": tenant_id,
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
        },
    }

    item = session.get(PostalEvent, event_id)
    if item:
        if item.tenant_id != tenant_id:
            raise HTTPException(409, "postal_event_tenant_conflict")
        if item.state == "delivered":
            return {"accepted": True, "duplicate": True}
    else:
        item = PostalEvent(
            id=event_id,
            event_type=canonical,
            correlation_id=correlation,
            message_id=local_message_id,
            tenant_id=tenant_id,
            payload=json.dumps(normalized, separators=(",", ":"), sort_keys=True),
        )
        session.add(item)
    item.attempts = (item.attempts or 0) + 1
    item.updated_at = datetime.now(UTC)
    session.commit()

    main.persist_email_event(
        session,
        event_id=event_id,
        tenant_id=tenant_id,
        message_id=local_message_id,
        correlation_id=correlation,
        event_type=canonical,
        recipient=recipient,
        raw_status=normalized.get("status"),
        payload=item.payload,
    )
    item = session.get(PostalEvent, event_id)
    item.payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    session.commit()

    delivered = await main.emit_middleware(
        "klyrow." + canonical,
        {**normalized, "customer_id": tenant_id},
    )
    item = session.get(PostalEvent, event_id)
    if delivered:
        item.state = "delivered"
        item.last_error = None
        session.commit()
        main.MAIL.labels(canonical.rsplit(".", 1)[-1]).inc()
        return {"accepted": True}

    item.state = "dlq" if item.attempts >= 5 else "retry"
    item.last_error = "middleware_delivery_failed"
    session.commit()
    raise HTTPException(503, "middleware_delivery_pending")
