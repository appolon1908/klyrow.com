"""Validate Middleware's command-bound transactional production attestation.

Middleware owns production policy. Klyrow owns delivery. This module is the
small fail-closed contract between those authorities; it does not persist or
mutate policy and therefore cannot create a parallel control plane.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ProductionAuthorizationError(ValueError):
    pass


class CommandBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messageId: str = Field(min_length=8, max_length=200)
    correlationId: str = Field(min_length=8, max_length=200)
    idempotencyKeySha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sender: str = Field(min_length=3, max_length=320)
    recipientsSha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MiddlewareProductionAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal["1.0"]
    tenantId: str = Field(min_length=1, max_length=120)
    policyVersion: int = Field(ge=1)
    mode: Literal["TRANSACTIONAL_CANARY", "TRANSACTIONAL_PRODUCTION"]
    authorizationState: Literal["ACTIVE"]
    killSwitchOpen: Literal[True]
    changeId: str = Field(min_length=3, max_length=200)
    category: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,119}$")
    validFrom: datetime
    validUntil: datetime
    provider: Literal["klyrow-postal"]
    environment: Literal["production"]
    approvedReleaseSha: str = Field(pattern=r"^[0-9a-f]{40}$")
    authorizationTimestamp: datetime
    activationTimestamp: datetime
    commandBinding: CommandBinding


def _recipients_sha256(recipients: list[str]) -> str:
    """Hash canonical addresses independently of order, retaining duplicates."""
    return hashlib.sha256(
        json.dumps(
            sorted(value.strip().lower() for value in recipients),
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _utc_datetime(value: datetime, error: str) -> datetime:
    """Reject undefined offsets and compare instants without host-local time."""
    try:
        if isinstance(value, datetime) and value.utcoffset() is not None:
            return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        pass
    raise ProductionAuthorizationError(error)


def validate_production_authorization(
    value: Any,
    *,
    tenant_id: str,
    message_id: str,
    correlation_id: str,
    sender: str,
    recipients: list[str],
    category: str,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        authority = MiddlewareProductionAuthorization.model_validate(value)
    except ValidationError as exc:
        raise ProductionAuthorizationError("production_authorization_invalid") from exc

    current = _utc_datetime(
        now if now is not None else datetime.now(timezone.utc),
        "production_authorization_clock_invalid",
    )
    valid_from = _utc_datetime(
        authority.validFrom, "production_authorization_window_invalid"
    )
    valid_until = _utc_datetime(
        authority.validUntil, "production_authorization_window_invalid"
    )
    if not valid_from <= current < valid_until:
        raise ProductionAuthorizationError("production_authorization_outside_window")
    authorized_at = _utc_datetime(
        authority.authorizationTimestamp, "production_authorization_timestamps_invalid"
    )
    activated_at = _utc_datetime(
        authority.activationTimestamp, "production_authorization_timestamps_invalid"
    )
    if authorized_at > activated_at or activated_at > current:
        raise ProductionAuthorizationError("production_authorization_timestamps_invalid")

    binding = authority.commandBinding
    expected = {
        "tenant": tenant_id,
        "message": message_id,
        "correlation": correlation_id,
        "sender": sender.lower(),
        "recipients": _recipients_sha256(recipients),
        "category": category.lower(),
    }
    actual = {
        "tenant": authority.tenantId,
        "message": binding.messageId,
        "correlation": binding.correlationId,
        "sender": binding.sender.lower(),
        "recipients": binding.recipientsSha256,
        "category": authority.category,
    }
    if actual != expected:
        raise ProductionAuthorizationError("production_authorization_binding_mismatch")
    if idempotency_key is not None and binding.idempotencyKeySha256 != hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest():
        raise ProductionAuthorizationError("production_authorization_idempotency_mismatch")
    return authority.model_dump(mode="json")


def provider_payload_from_outbox(
    value: dict[str, Any],
    *,
    tenant_id: str,
    message_id: str,
    correlation_id: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    payload = dict(value)
    authority = payload.pop("_codestra_production_authorization", None)
    if authority is None:
        return payload, False
    category = authority.get("category") if isinstance(authority, dict) else ""
    validate_production_authorization(
        authority,
        tenant_id=tenant_id,
        message_id=message_id,
        correlation_id=correlation_id,
        sender=str(payload.get("from") or ""),
        recipients=[str(value) for value in payload.get("to", [])],
        category=str(category or ""),
        now=now,
    )
    return payload, True


__all__ = [
    "MiddlewareProductionAuthorization",
    "ProductionAuthorizationError",
    "provider_payload_from_outbox",
    "validate_production_authorization",
]
