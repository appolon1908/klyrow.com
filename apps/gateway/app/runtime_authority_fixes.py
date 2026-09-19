"""Runtime parity fixes for the canonical Klyrow API authority.

The production composition root installs these narrow fixes after all source
routers are loaded. They preserve the existing route table while correcting
cross-cutting authority defects:

* scoped storage digests never replace caller correlation identifiers;
* a historical raw-key replay is accepted only when the row resolves to a real
  message in the same tenant, never for campaign or other resource records;
* deployed legacy Server A Odoo webhook routes remain delivery eligible;
* blocked dead letters remain visible without starving later recoverable work.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select

from . import main as core
from . import production_api
from . import tenancy_onboarding
from . import webmail
from .durable_results import read_control_response, seal_control_response
from .provider_reconciliation_fixes import install_provider_reconciliation_fixes

_ORIGINAL_OPERATION_JSON = production_api._operation_json
_INSTALLED = False


def _legacy_message_shape(record: Any) -> dict[str, Any] | None:
    try:
        response = read_control_response(record)
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(response, dict):
        return None
    if response.get("id") != getattr(record, "resource_id", None):
        return None
    required = {"provider_message_id", "status", "safe_mode", "stream"}
    if not required.issubset(response):
        return None
    if response.get("provider_message_id") != response.get("id"):
        return None
    return response


def legacy_message_send_response(
    record: Any,
    session: Any,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Return a response only for a tenant-owned historical message send.

    Shape alone is insufficient because campaigns and message sends historically
    shared the same raw-key table. The referenced Message must still exist in
    the same tenant before the compatibility row can shadow a scoped key.
    """

    if record is None:
        return None
    if getattr(record, "tenant_id", tenant_id) != tenant_id:
        return None
    response = _legacy_message_shape(record)
    if response is None:
        return None
    resource_id = str(response["id"])
    persisted_message_id = session.scalar(
        select(core.Message.id).where(
            core.Message.id == resource_id,
            core.Message.tenant_id == tenant_id,
        )
    )
    if persisted_message_id != resource_id:
        return None
    return response


def operation_json_with_correlation(item: Any, session: Any, **kwargs: Any) -> dict[str, Any]:
    """Preserve an IntegrationOutbox envelope correlation ID in responses."""

    result = _ORIGINAL_OPERATION_JSON(item, session, **kwargs)
    payload_json = getattr(item, "payload_json", None)
    if not payload_json:
        return result
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return result
    if not isinstance(payload, dict):
        return result
    envelope = payload.get("envelope")
    if not isinstance(envelope, dict):
        return result
    correlation_id = envelope.get("correlation_id")
    if isinstance(correlation_id, str) and correlation_id.strip():
        result["correlation_id"] = correlation_id.strip()
    return result


async def send_with_scoped_legacy_compatibility(
    message: core.MailIn,
    context: dict[str, Any],
    session: Any,
    idempotency_key: Optional[str],
    *,
    _production_authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the existing send flow with action-safe legacy compatibility."""

    if not idempotency_key:
        raise HTTPException(400, "idempotency_key_required")

    resource = "messages"
    storage_key = core.scoped_idempotency_key(
        context,
        idempotency_key,
        action="message.send",
        resource=resource,
    )
    request_hash = core.semantic_request_hash(
        action="message.send",
        resource=resource,
        payload=message.model_dump(mode="json"),
    )
    prior = session.scalar(
        select(core.Idempotency).where(
            core.Idempotency.key == storage_key,
            core.Idempotency.tenant_id == context["tenant"],
        )
    )
    if prior is None:
        legacy = session.scalar(
            select(core.Idempotency).where(
                core.Idempotency.key == idempotency_key,
                core.Idempotency.tenant_id == context["tenant"],
            )
        )
        legacy_response = legacy_message_send_response(
            legacy,
            session,
            context["tenant"],
        )
        if legacy_response is not None:
            if legacy.request_hash != core.sha(message.model_dump_json()):
                raise HTTPException(409, "idempotency_key_payload_mismatch")
            return legacy_response
    if prior is not None:
        if prior.request_hash != request_hash:
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        return read_control_response(prior)

    from .operations import enforce_tenant_send_gate

    enforce_tenant_send_gate(session, context["tenant"])

    from .agent_mailboxes import authorize_agent_sender

    authorize_agent_sender(
        session,
        context,
        message.sender,
        message.campaign_id,
        message.reply_to,
    )

    from .guards import authorize_send

    authorization = authorize_send(
        session,
        tenant_id=context["tenant"],
        sender=str(message.sender),
        recipient=str(message.to),
        stream=message.stream,
        sandbox=core.SAFE_MODE,
        campaign_id=message.campaign_id,
        topic=message.topic,
    )
    if _production_authorization is None:
        core.enforce_production_canary(message, session)
    if message.stream == "marketing" and (
        message.campaign_id or not core.SAFE_MODE
    ):
        core.enforce_campaign_canary(message, context, session)

    sender = message.sender.lower()
    domain = sender.rsplit("@", 1)[1]

    from .delivery_controls import enforce_delivery_controls

    enforce_delivery_controls(
        session,
        context["tenant"],
        sender,
        message.stream,
    )
    allowed = session.scalar(
        select(core.Domain).where(
            core.Domain.tenant_id == context["tenant"],
            core.Domain.domain == domain,
            core.Domain.verified == True,
        )
    )
    if not allowed:
        raise HTTPException(422, "sender_domain_not_verified")
    if context.get("role") != "codestra-email-agent":
        exact = session.scalar(
            select(core.AllowedSender).where(
                core.AllowedSender.tenant_id == context["tenant"],
                core.AllowedSender.address == sender,
                core.AllowedSender.enabled == True,
            )
        )
        if not exact:
            raise HTTPException(403, "sender_address_not_allowed")

    since = datetime.now(timezone.utc) - timedelta(days=1)
    count = len(
        session.scalars(
            select(core.Message).where(
                core.Message.tenant_id == context["tenant"],
                core.Message.created_at >= since,
            )
        ).all()
    )
    tenant = session.get(core.Tenant, context["tenant"])
    if count >= tenant.quota:
        raise HTTPException(429, "daily_quota_exceeded")

    from .billing import UsageEvent
    from .guards import billing_identity

    subscription_id, price_id = billing_identity(
        session,
        context["tenant"],
        sandbox=core.SAFE_MODE,
    )
    message_id = str(uuid.uuid4())
    status = "accepted" if core.SAFE_MODE else "queued"
    operation_id = str(
        message.callback_metadata.get("operation_id") or message_id
    )
    correlation_id = str(
        message.callback_metadata.get("correlation_id") or message_id
    )
    result = {
        "id": message_id,
        "provider_message_id": message_id,
        "status": status,
        "safe_mode": core.SAFE_MODE,
        "stream": message.stream,
    }

    session.add(
        core.Message(
            id=message_id,
            tenant_id=context["tenant"],
            recipient=authorization["recipient"],
            sender=authorization["sender"],
            subject=message.subject,
            status=status,
        )
    )
    session.add(
        core.Event(
            id=str(uuid.uuid4()),
            tenant_id=context["tenant"],
            message_id=message_id,
            kind="klyrow.email.accepted",
            payload=json.dumps({"stream": message.stream}),
        )
    )
    core.queue_email_lifecycle_event(
        session,
        kind="email.accepted",
        tenant_id=context["tenant"],
        message_id=message_id,
        operation_id=operation_id,
        correlation_id=correlation_id,
        recipient=message.to.lower(),
    )
    session.add(
        core.Idempotency(
            key=storage_key,
            tenant_id=context["tenant"],
            request_hash=request_hash,
            resource_id=message_id,
            response_json=seal_control_response(result, tenant_id=context["tenant"], storage_key=storage_key, request_hash=request_hash, resource_id=message_id),
        )
    )
    session.add(
        UsageEvent(
            id=str(uuid.uuid4()),
            tenant_id=context["tenant"],
            subscription_id=subscription_id,
            message_id=message_id,
            event_key="accepted:api:" + storage_key,
            unit="accepted_message",
            quantity=1,
            price_id=price_id,
        )
    )

    if not core.SAFE_MODE:
        from .preferences import one_click_unsubscribe_headers

        delivery_payload: dict[str, Any] = {
            "to": [str(message.to)],
            "from": str(message.sender),
            "subject": message.subject,
            "html_body": message.html,
            "plain_body": message.text,
            "campaign_id": message.campaign_id,
            "stream": message.stream,
        }
        if _production_authorization is not None:
            delivery_payload["_codestra_production_authorization"] = (
                _production_authorization
            )
        delivery_headers = dict(message.headers)
        if message.stream == "marketing":
            delivery_headers.update(
                one_click_unsubscribe_headers(
                    context["tenant"],
                    str(message.to),
                )
            )
        if delivery_headers:
            delivery_payload["headers"] = delivery_headers

        from .guards import stream_priority

        session.add(
            core.EmailOutbox(
                id=str(uuid.uuid4()),
                tenant_id=context["tenant"],
                message_id=message_id,
                operation_id=operation_id,
                correlation_id=correlation_id,
                payload=json.dumps(
                    delivery_payload,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                priority=stream_priority(message.stream),
            )
        )
        core.queue_email_lifecycle_event(
            session,
            kind="email.queued",
            tenant_id=context["tenant"],
            message_id=message_id,
            operation_id=operation_id,
            correlation_id=correlation_id,
            recipient=message.to.lower(),
        )

    session.commit()
    core.MAIL.labels("queued").inc()
    return result


def install_runtime_authority_fixes() -> None:
    """Install the fixes once into all modules that captured old helpers."""

    global _INSTALLED
    if _INSTALLED:
        return
    core._send = send_with_scoped_legacy_compatibility
    tenancy_onboarding._send = send_with_scoped_legacy_compatibility
    webmail._send = send_with_scoped_legacy_compatibility
    production_api._operation_json = operation_json_with_correlation
    install_provider_reconciliation_fixes()
    _INSTALLED = True


__all__ = [
    "install_runtime_authority_fixes",
    "legacy_message_send_response",
    "operation_json_with_correlation",
    "send_with_scoped_legacy_compatibility",
]
