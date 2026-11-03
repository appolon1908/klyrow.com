"""Fail-closed policy helpers for governed SMTP submissions.

SMTP has no application-level stream field, so a credential must be restricted to
exactly one approved stream. Keycloak password-recovery credentials are scoped to
``SECURITY`` and remain sandbox-only unless every dedicated activation gate is
explicitly enabled.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

APPROVED_STREAMS = frozenset({"TRANSACTIONAL", "SECURITY", "SYSTEM", "MARKETING", "BULK"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SmtpPolicyError(ValueError):
    """Raised when SMTP credential or activation policy is invalid."""


def _boolean(name: str, environment: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environment is None else environment
    value = str(source.get(name, "false")).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise SmtpPolicyError(f"{name} must be a boolean value")


def parse_allowed_streams(raw: str) -> frozenset[str]:
    """Parse a credential stream allowlist and reject unknown or duplicate data."""

    if not isinstance(raw, str):
        raise SmtpPolicyError("allowed streams must be encoded as JSON")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SmtpPolicyError("allowed streams must be valid JSON") from exc
    if not isinstance(decoded, list) or not decoded:
        raise SmtpPolicyError("allowed streams must be a non-empty JSON array")
    if any(not isinstance(item, str) or not item.strip() for item in decoded):
        raise SmtpPolicyError("allowed streams must contain non-empty strings")

    normalized = [item.strip().upper() for item in decoded]
    if len(normalized) != len(set(normalized)):
        raise SmtpPolicyError("allowed streams must not contain duplicates")
    unknown = set(normalized) - APPROVED_STREAMS
    if unknown:
        raise SmtpPolicyError(f"unsupported SMTP streams: {sorted(unknown)}")
    return frozenset(normalized)


def select_credential_stream(raw: str) -> str:
    """Return the single stream assigned to a dedicated SMTP credential."""

    streams = parse_allowed_streams(raw)
    if len(streams) != 1:
        raise SmtpPolicyError("SMTP credentials must be dedicated to exactly one message stream")
    return next(iter(streams))


def security_stream_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    return _boolean("KLYROW_SECURITY_SMTP_ENABLED", environment)


def security_live_delivery_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    return security_stream_enabled(environment) and _boolean(
        "KLYROW_SECURITY_SMTP_LIVE_ENABLED", environment
    )


def security_production_approved(
    environment: Mapping[str, str] | None = None,
) -> bool:
    return _boolean("KLYROW_SECURITY_SMTP_PRODUCTION_APPROVED", environment)


def security_canary_recipients(
    environment: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return the exact lower-case recipient allowlist for pre-production canaries."""

    source = os.environ if environment is None else environment
    raw = str(source.get("KLYROW_SECURITY_SMTP_CANARY_RECIPIENTS", "")).strip()
    if not raw:
        return frozenset()
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if len(values) != len(set(values)):
        raise SmtpPolicyError("SECURITY canary recipients must not contain duplicates")
    if len(values) > 10:
        raise SmtpPolicyError("SECURITY canary recipients are limited to ten addresses")
    if any(not _EMAIL.fullmatch(item) for item in values):
        raise SmtpPolicyError("SECURITY canary recipient is not a valid email address")
    return frozenset(values)


def security_canary_max_deliveries(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return a bounded canary delivery allowance; production uses no canary cap."""

    if security_production_approved(environment):
        return 0
    source = os.environ if environment is None else environment
    raw = str(source.get("KLYROW_SECURITY_SMTP_CANARY_MAX_DELIVERIES", "1")).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SmtpPolicyError(
            "KLYROW_SECURITY_SMTP_CANARY_MAX_DELIVERIES must be an integer"
        ) from exc
    if not 1 <= value <= 10:
        raise SmtpPolicyError("KLYROW_SECURITY_SMTP_CANARY_MAX_DELIVERIES must be between 1 and 10")
    return value


def security_recipient_allowed(
    recipient: str,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Authorize an external SECURITY recipient under canary or production policy."""

    if not security_live_delivery_enabled(environment):
        return False
    if security_production_approved(environment):
        return True
    normalized = recipient.strip().lower()
    return normalized in security_canary_recipients(environment)


def effective_sandbox(
    *,
    stream: str,
    tenant_sandbox_mode: bool,
    tenant_sending_disabled: bool,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether a submission must stay in the internal sandbox.

    Existing non-security SMTP submission remains sandbox-only. SECURITY mail
    may leave the sandbox only when both dedicated environment gates are true
    and the tenant policy has independently enabled sending.
    """

    normalized = stream.strip().upper()
    if normalized not in APPROVED_STREAMS:
        raise SmtpPolicyError("unsupported SMTP stream")
    if normalized != "SECURITY":
        return True
    if not security_stream_enabled(environment):
        raise SmtpPolicyError("SECURITY SMTP stream is disabled")
    if not security_live_delivery_enabled(environment):
        return True
    if tenant_sandbox_mode or tenant_sending_disabled:
        raise SmtpPolicyError(
            "SECURITY live delivery requires an enabled non-sandbox tenant policy"
        )
    return False
