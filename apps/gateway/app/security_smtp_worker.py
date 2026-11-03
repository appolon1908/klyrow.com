"""Dedicated Postal delivery worker for Keycloak SECURITY SMTP messages.

The worker is inert for outbound delivery unless the two Klyrow security-mail
gates are enabled. SECURITY MIME is encrypted while queued and scrubbed from
Klyrow persistence after provider submission or any terminal outcome. Only
privacy-safe operational evidence remains after the retry window.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from .main import DB
from .provider import ProviderAudit, ProviderEvent, ProviderMessage, SandboxCapture, now
from .provider_outcome import INDETERMINATE, provider_outcome_is_ambiguous, reconcile_before_retry
from .security_payload import (
    decrypt_security_payload,
    max_payload_age_seconds,
    scrubbed_security_payload,
)
from .smtp_policy import security_live_delivery_enabled

MAX_ATTEMPTS = 5
LEASE_SECONDS = 120


def _decode_part(part: Message) -> str:
    try:
        content = part.get_content()
    except (LookupError, UnicodeError, ValueError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    if isinstance(content, bytes):
        return content.decode(part.get_content_charset() or "utf-8", errors="replace")
    return str(content)


def parse_postal_body(raw: bytes) -> tuple[str, str]:
    """Extract plain and HTML bodies without including attachments."""

    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    candidates = message.walk() if message.is_multipart() else [message]
    for part in candidates:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().lower()
        if content_type == "text/plain":
            plain_parts.append(_decode_part(part))
        elif content_type == "text/html":
            html_parts.append(_decode_part(part))
    return "\n".join(plain_parts), "\n".join(html_parts)


def postal_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = decrypt_security_payload(snapshot["payload"])
    plain_body, html_body = parse_postal_body(raw)
    if not plain_body and not html_body:
        raise ValueError("security SMTP message has no deliverable text body")
    payload: dict[str, Any] = {
        "to": [snapshot["recipient"]],
        "from": snapshot["sender"],
        "subject": snapshot["subject"],
        "tag": snapshot["correlation_id"],
        "stream": "security",
    }
    if plain_body:
        payload["plain_body"] = plain_body
    if html_body:
        payload["html_body"] = html_body
    return payload


def _decoded_payload(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _contains_security_ciphertext(payload: dict[str, Any]) -> bool:
    return payload.get("stream") == "SECURITY" and bool(payload.get("encrypted_raw"))


def _purge_expired_security_payloads() -> int:
    """Bound SECURITY ciphertext retention across every terminal and retry path.

    Generic sandbox delivery and generic lease recovery do not know how to scrub
    the SECURITY envelope. This retention pass therefore covers active retries,
    sandbox DELIVERED rows/captures, and lease-recovery DEAD_LETTER rows. Active
    messages that outlive the security payload window are failed closed; terminal
    states keep their delivery outcome while only the sensitive body is purged.
    """

    cutoff = now() - timedelta(seconds=max_payload_age_seconds())
    purged = 0
    active_states = {"QUEUED", "DEFERRED", "PROCESSING"}
    terminal_states = {"SUBMITTED", "SENT", "DELIVERED", "FAILED", "DEAD_LETTER"}
    with DB() as session:
        items = session.scalars(
            select(ProviderMessage).where(
                ProviderMessage.stream == "SECURITY",
                ProviderMessage.status.in_(sorted(active_states | terminal_states)),
                ProviderMessage.created_at < cutoff,
            )
        ).all()
        for item in items:
            payload = _decoded_payload(item.payload_json)
            capture = session.scalar(
                select(SandboxCapture).where(SandboxCapture.message_id == item.id)
            )
            capture_payload = _decoded_payload(capture.content_json) if capture else {}
            had_sensitive_body = _contains_security_ciphertext(payload)
            had_sensitive_capture = _contains_security_ciphertext(capture_payload)
            if not had_sensitive_body and not had_sensitive_capture:
                continue

            safe_json = scrubbed_security_payload(
                payload or capture_payload,
                reason="payload_retention_expired",
            )
            if had_sensitive_body:
                item.payload_json = safe_json
            if capture and had_sensitive_capture:
                capture.content_json = safe_json

            if item.status in active_states:
                item.status = "DEAD_LETTER"
                item.lease_expires_at = None
                item.last_error = "security_payload_retention_expired"
            item.updated_at = now()
            session.add(
                ProviderAudit(
                    id=str(uuid.uuid4()),
                    tenant_id=item.tenant_id,
                    actor="worker:klyrow-security-smtp",
                    action="smtp.security.payload.purged",
                    outcome=("dead_letter" if item.status == "DEAD_LETTER" else "retained_status"),
                    correlation_id=item.correlation_id,
                    resource_id=item.id,
                )
            )
            purged += 1
        session.commit()
    return purged


def _claim_one() -> dict[str, Any] | None:
    with DB() as session:
        item = session.scalar(
            select(ProviderMessage)
            .where(
                ProviderMessage.stream == "SECURITY",
                ProviderMessage.sandbox.is_(False),
                ProviderMessage.status.in_(["QUEUED", "DEFERRED"]),
                ProviderMessage.available_at <= now(),
                ProviderMessage.attempts < MAX_ATTEMPTS,
            )
            .order_by(ProviderMessage.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not item:
            return None
        item.status = "PROCESSING"
        item.attempts += 1
        item.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
        item.updated_at = now()
        snapshot = {
            "id": item.id,
            "tenant_id": item.tenant_id,
            "sender": item.sender,
            "recipient": item.recipient,
            "subject": item.subject,
            "correlation_id": item.correlation_id,
            "idempotency_key": item.idempotency_key,
            "attempts": item.attempts,
            "payload": json.loads(item.payload_json),
        }
        session.commit()
        return snapshot


def _postal_key() -> str:
    key_file = os.getenv("KLYROW_POSTAL_API_KEY_FILE", "").strip()
    if not key_file:
        raise RuntimeError("Postal credential path is not configured")
    key = Path(key_file).read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("Postal credential is unavailable")
    return key


def _mark_submitted(snapshot: dict[str, Any], provider_message_id: str) -> None:
    with DB() as session:
        item = session.get(ProviderMessage, snapshot["id"])
        if not item or item.status != "PROCESSING":
            return
        item.status = "SUBMITTED"
        item.provider_message_id = provider_message_id
        item.payload_json = scrubbed_security_payload(
            snapshot["payload"],
            reason="provider_submitted",
        )
        item.lease_expires_at = None
        item.last_error = None
        item.updated_at = now()
        event_id = str(uuid.uuid4())
        session.add(
            ProviderEvent(
                id=event_id,
                tenant_id=item.tenant_id,
                message_id=item.id,
                kind="message.submitted",
                payload_json=json.dumps(
                    {
                        "event_id": event_id,
                        "tenant_id": item.tenant_id,
                        "message_id": item.id,
                        "provider_message_id": provider_message_id,
                        "correlation_id": item.correlation_id,
                        "event": "message.submitted",
                        "provider": "postal",
                        "stream": "SECURITY",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        )
        session.add(
            ProviderAudit(
                id=str(uuid.uuid4()),
                tenant_id=item.tenant_id,
                actor="worker:klyrow-security-smtp",
                action="smtp.security.message.submitted",
                outcome="accepted",
                correlation_id=item.correlation_id,
                resource_id=item.id,
            )
        )
        session.commit()


def _mark_failed(snapshot: dict[str, Any], error_name: str) -> None:
    with DB() as session:
        item = session.get(ProviderMessage, snapshot["id"])
        if not item or item.status != "PROCESSING":
            return
        exhausted = item.attempts >= MAX_ATTEMPTS
        item.status = "DEAD_LETTER" if exhausted else "DEFERRED"
        item.available_at = now() + timedelta(seconds=min(300, 2 ** max(item.attempts, 1)))
        item.lease_expires_at = None
        item.last_error = error_name[:120]
        if exhausted:
            item.payload_json = scrubbed_security_payload(
                snapshot["payload"],
                reason="delivery_attempts_exhausted",
            )
        item.updated_at = now()
        session.add(
            ProviderAudit(
                id=str(uuid.uuid4()),
                tenant_id=item.tenant_id,
                actor="worker:klyrow-security-smtp",
                action="smtp.security.message.delivery_failed",
                outcome="dead_letter" if exhausted else "retry",
                correlation_id=item.correlation_id,
                resource_id=item.id,
            )
        )
        session.commit()


def _mark_indeterminate(snapshot: dict[str, Any], error_name: str) -> None:
    """Quarantine an ambiguous Postal result until reconciliation proves absence."""

    with DB() as session:
        item = session.get(ProviderMessage, snapshot["id"])
        if not item or item.status != "PROCESSING":
            return
        if not reconcile_before_retry(
            state=INDETERMINATE,
            provider_message_id=item.provider_message_id,
            provider_absence_confirmed=False,
        ):
            item.status = INDETERMINATE
            item.lease_expires_at = None
            item.last_error = error_name[:120]
            item.updated_at = now()
            session.add(
                ProviderAudit(
                    id=str(uuid.uuid4()),
                    tenant_id=item.tenant_id,
                    actor="worker:klyrow-security-smtp",
                    action="smtp.security.message.indeterminate",
                    outcome="reconciliation_required",
                    correlation_id=item.correlation_id,
                    resource_id=item.id,
                )
            )
            session.commit()


async def process_one_security_message() -> bool:
    """Submit one eligible SECURITY message to Postal."""

    _purge_expired_security_payloads()
    if not security_live_delivery_enabled():
        return False
    snapshot = _claim_one()
    if snapshot is None:
        return False
    try:
        base_url = os.environ["KLYROW_POSTAL_API_URL"].rstrip("/")
        headers = {
            "X-Server-API-Key": _postal_key(),
            "Idempotency-Key": "klyrow-security:" + snapshot["id"],
        }
        postal_host = os.getenv("KLYROW_POSTAL_API_HOST_HEADER", "").strip()
        if postal_host:
            headers["Host"] = postal_host
        async with httpx.AsyncClient(
            timeout=10,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                base_url + "/api/v1/send/message",
                headers=headers,
                json=postal_payload(snapshot),
            )
            response.raise_for_status()
            body = response.json()
        provider_message_id = str(body.get("data", {}).get("message_id") or snapshot["id"])
        _mark_submitted(snapshot, provider_message_id)
        return True
    except Exception as exc:  # noqa: BLE001 - error type only is persisted/logged
        if provider_outcome_is_ambiguous(exc):
            _mark_indeterminate(snapshot, type(exc).__name__)
        else:
            _mark_failed(snapshot, type(exc).__name__)
        print(
            json.dumps(
                {
                    "level": "warning",
                    "system": "klyrow-security-smtp",
                    "event": "postal_submission_failed",
                    "message_id": snapshot["id"],
                    "correlation_id": snapshot["correlation_id"],
                    "error": type(exc).__name__,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return False


async def security_smtp_delivery_loop() -> None:
    while True:
        try:
            processed = await process_one_security_message()
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            print(
                json.dumps(
                    {
                        "level": "error",
                        "system": "klyrow-security-smtp",
                        "event": "worker_tick_failed",
                        "error": type(exc).__name__,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            processed = False
        await asyncio.sleep(1 if processed else 3)
