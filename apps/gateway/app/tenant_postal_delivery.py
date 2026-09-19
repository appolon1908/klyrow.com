"""Tenant-scoped Postal delivery with callback-attribution metadata."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta

from .telemetry import traced, trace_carrier, stored_carrier

import httpx
from sqlalchemy import or_, select

from .main import (
    DB,
    EmailOutbox,
    Event,
    Message,
    ProductionCanaryGate,
    SAFE_MODE,
    campaign_execution_mode,
    campaign_worker_payload_allowed,
    canary_configuration,
    canary_gate_key,
    canary_payload_allowed,
    set_core_message_status,
)
from .postal_provisioning import now, tenant_postal_api_key
from .provider_outcome import INDETERMINATE, provider_outcome_is_ambiguous, reconcile_before_retry


def attributed_postal_payload(serialized_payload: str, message_id: str) -> dict:
    """Add the opaque local message ID that Postal echoes as the signed tag."""

    payload = json.loads(serialized_payload)
    if not isinstance(payload, dict):
        raise ValueError("invalid_outbox_payload")
    payload["tag"] = message_id
    return payload


async def tenant_email_outbox_loop() -> None:
    """Deliver with tenant credentials and an opaque local callback tag.

    Postal echoes the request tag in signed lifecycle callbacks. The tag is the
    globally unique local message ID, never a tenant ID or other authorization
    assertion. Callback handling resolves the tag/provider ID against Klyrow's
    own outbound records and fails closed when attribution is unavailable.
    """

    while True:
        await asyncio.sleep(2)
        if SAFE_MODE:
            continue
        snapshot = None
        try:
            with DB() as session:
                stale = now() - timedelta(minutes=5)
                current = now()
                abandoned = session.scalar(
                    select(EmailOutbox).where(EmailOutbox.state == "sending", EmailOutbox.updated_at < stale).order_by(EmailOutbox.updated_at).with_for_update(skip_locked=True)
                )
                if abandoned and not reconcile_before_retry(state=INDETERMINATE, provider_message_id=abandoned.provider_message_id, provider_absence_confirmed=False):
                    abandoned.state = INDETERMINATE
                    abandoned.last_error = "abandoned_submission_requires_reconciliation"
                    abandoned.next_attempt_at = None
                    abandoned.updated_at = current
                    message = session.get(Message, abandoned.message_id)
                    if message:
                        set_core_message_status(message, "indeterminate")
                    session.commit()
                    continue
                item = session.scalar(
                    select(EmailOutbox)
                    .where(
                        or_(
                            EmailOutbox.state == "pending",
                            (EmailOutbox.state == "retry")
                            & or_(
                                EmailOutbox.next_attempt_at.is_(None),
                                EmailOutbox.next_attempt_at <= current,
                            ),
                        ),
                        EmailOutbox.attempts < 5,
                    )
                    .order_by(EmailOutbox.priority, EmailOutbox.created_at)
                    .with_for_update(skip_locked=True)
                )
                if not item:
                    continue
                try:
                    payload = json.loads(item.payload)
                except (TypeError, ValueError):
                    item.state = "quarantined"
                    item.last_error = "invalid_outbox_payload"
                    item.updated_at = current
                    session.commit()
                    continue
                if not isinstance(payload, dict):
                    item.state = "quarantined"
                    item.last_error = "invalid_outbox_payload"
                    item.updated_at = current
                    session.commit()
                    continue

                campaign_payload = payload.get("stream") == "marketing"
                campaign_production = (
                    campaign_payload
                    and campaign_execution_mode() == "CAMPAIGN_PRODUCTION_ENABLED"
                )
                gate_key = (
                    "campaign:" + str(payload.get("campaign_id"))
                    if campaign_payload
                    else canary_gate_key()
                )
                gate = (
                    None
                    if campaign_production
                    else session.scalar(
                        select(ProductionCanaryGate)
                        .where(ProductionCanaryGate.gate_key == gate_key)
                        .with_for_update()
                    )
                )
                maximum = 1 if campaign_payload else canary_configuration()[3]
                first_attempt = (item.attempts or 0) == 0
                reservation_denied = False if campaign_production else (
                    not gate
                    or (
                        first_attempt
                        and (
                            gate.claimed_deliveries >= gate.reserved_deliveries
                            or gate.claimed_deliveries >= maximum
                        )
                    )
                )
                payload_allowed = (
                    campaign_worker_payload_allowed(payload, item.tenant_id)
                    if campaign_payload
                    else canary_payload_allowed(payload)
                )
                if not payload_allowed or reservation_denied:
                    item.state = "quarantined"
                    item.last_error = "production_canary_policy_denied"
                    item.updated_at = current
                    message = session.get(Message, item.message_id)
                    if message:
                        set_core_message_status(message, "suppressed")
                    session.commit()
                    continue
                if first_attempt and gate:
                    gate.claimed_deliveries += 1
                    gate.updated_at = current

                sender = str(payload.get("from") or "").strip().lower()
                sender_domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
                key = tenant_postal_api_key(
                    session, item.tenant_id, sender_domain=sender_domain
                )
                item.state = "sending"
                item.attempts += 1
                item.next_attempt_at = None
                item.updated_at = current
                message = session.get(Message, item.message_id)
                if message:
                    set_core_message_status(message, "submitted")
                snapshot = (
                    item.id,
                    item.message_id,
                    item.tenant_id,
                    item.payload,
                    key,
                    item.trace_context_json,
                )
                session.commit()

            request_payload = attributed_postal_payload(snapshot[3], snapshot[1])
            headers = {
                "X-Server-API-Key": snapshot[4],
                "Idempotency-Key": "klyrow:" + snapshot[1],
            }
            postal_host = os.getenv("KLYROW_POSTAL_API_HOST_HEADER", "").strip()
            if postal_host:
                headers["Host"] = postal_host
            with traced("postal tenant submit", stored_carrier(snapshot[5])):
                headers.update(trace_carrier())
                async with httpx.AsyncClient(
                    timeout=10, trust_env=False, follow_redirects=False
                ) as client:
                    response = await client.post(
                        os.environ["KLYROW_POSTAL_API_URL"] + "/api/v1/send/message",
                        headers=headers,
                        json=request_payload,
                    )
                    response.raise_for_status()
                    provider_id = str(
                        response.json().get("data", {}).get("message_id")
                        or snapshot[1]
                    )
            with DB() as session:
                item = session.get(EmailOutbox, snapshot[0])
                message = session.get(Message, snapshot[1])
                if item:
                    item.state = "delivered"
                    item.provider_message_id = provider_id
                    item.last_error = None
                    item.updated_at = now()
                if message:
                    set_core_message_status(message, "provider_accepted")
                from .webmail import update_outbound_status

                update_outbound_status(
                    session, snapshot[2], snapshot[1], "provider_accepted"
                )
                session.commit()
        except Exception as exc:
            with DB() as session:
                if snapshot is not None:
                    item = session.get(EmailOutbox, snapshot[0])
                    if item:
                        message = session.get(Message, item.message_id)
                        if provider_outcome_is_ambiguous(exc) and not reconcile_before_retry(state=INDETERMINATE, provider_message_id=item.provider_message_id, provider_absence_confirmed=False):
                            item.state = INDETERMINATE
                            item.last_error = type(exc).__name__
                            item.updated_at = now()
                            item.next_attempt_at = None
                            if message:
                                set_core_message_status(message, "indeterminate")
                            from .webmail import update_outbound_status

                            update_outbound_status(
                                session,
                                item.tenant_id,
                                item.message_id,
                                "indeterminate",
                            )
                        else:
                            failed = item.attempts >= 5
                            item.state = "failed" if failed else "retry"
                            item.last_error = type(exc).__name__
                            item.updated_at = now()
                            item.next_attempt_at = None if failed else item.updated_at + timedelta(seconds=min(300, 2 ** max(item.attempts, 1)))
                            if message:
                                set_core_message_status(message, "failed" if failed else "deferred")
                            from .webmail import update_outbound_status

                            update_outbound_status(
                                session,
                                item.tenant_id,
                                item.message_id,
                                "failed" if failed else "deferred",
                            )
                            if failed:
                                session.add(
                                    Event(
                                        id=str(uuid.uuid4()), tenant_id=item.tenant_id,
                                        message_id=item.message_id, kind="klyrow.email.failed",
                                        payload=json.dumps({"reason": "provider_retry_exhausted"}),
                                    )
                                )
                        session.commit()
            print(
                json.dumps(
                    {
                        "level": "warning",
                        "system": "klyrow",
                        "event": "tenant_email_outbox_delivery_failed",
                        "error": type(exc).__name__,
                    }
                )
            )
