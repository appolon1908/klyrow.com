"""Read-only runtime preflight for the Keycloak SECURITY SMTP path.

Run inside the Klyrow gateway/worker image after migrations. The command never
prints or reads the SMTP password; it verifies only policy, identity, domain,
credential metadata, and activation gates.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC
from typing import Any

from sqlalchemy import select

from .main import DB, Tenant
from .provider import (
    ProviderDomain,
    SenderIdentity,
    SmtpCredential,
    TenantMailPolicy,
    now,
)
from .smtp_policy import (
    SmtpPolicyError,
    security_canary_max_deliveries,
    security_canary_recipients,
    security_live_delivery_enabled,
    security_production_approved,
    security_stream_enabled,
    select_credential_stream,
)

EXPECTED_MODES = {"disabled", "canary", "production"}


class PreflightError(RuntimeError):
    """Raised when the runtime does not match the reviewed SECURITY SMTP mode."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise PreflightError(f"{name} is required")
    return value


def _mode() -> str:
    value = os.getenv("KLYROW_SECURITY_SMTP_EXPECTED_MODE", "disabled").strip().lower()
    if value not in EXPECTED_MODES:
        raise PreflightError(
            "KLYROW_SECURITY_SMTP_EXPECTED_MODE must be disabled, canary, or production"
        )
    return value


def _assert_flags(mode: str) -> tuple[bool, bool, bool]:
    try:
        enabled = security_stream_enabled()
        live = security_live_delivery_enabled()
        production = security_production_approved()
    except SmtpPolicyError as exc:
        raise PreflightError(str(exc)) from exc

    expected = {
        "disabled": (False, False, False),
        "canary": (True, True, False),
        "production": (True, True, True),
    }[mode]
    actual = (enabled, live, production)
    if actual != expected:
        raise PreflightError(
            f"activation flags do not match mode {mode}: expected={expected} actual={actual}"
        )
    return actual


def validate() -> dict[str, Any]:
    mode = _mode()
    enabled, live, production = _assert_flags(mode)
    tenant_id = _required("KLYROW_SECURITY_SMTP_TENANT_ID")
    username = _required("KLYROW_SECURITY_SMTP_USERNAME")
    sender_address = _required("KLYROW_SECURITY_SMTP_SENDER").lower()

    try:
        recipients = sorted(security_canary_recipients())
        canary_limit = security_canary_max_deliveries()
    except SmtpPolicyError as exc:
        raise PreflightError(str(exc)) from exc

    if mode == "canary":
        if not recipients:
            raise PreflightError("canary mode requires at least one exact recipient")
        if len(recipients) > canary_limit:
            raise PreflightError("canary recipient count exceeds the reviewed delivery allowance")
    elif mode == "production" and recipients:
        raise PreflightError("production mode must clear the temporary canary recipient allowlist")

    with DB() as session:
        tenant = session.get(Tenant, tenant_id)
        if not tenant or not tenant.enabled:
            raise PreflightError("SECURITY SMTP tenant is missing or disabled")

        credential = session.scalar(
            select(SmtpCredential).where(SmtpCredential.username == username)
        )
        if not credential or credential.tenant_id != tenant_id or credential.status != "ACTIVE":
            raise PreflightError("dedicated SMTP credential is missing or inactive")
        expiry = credential.expires_at
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry and expiry <= now():
            raise PreflightError("dedicated SMTP credential is expired")
        try:
            stream = select_credential_stream(credential.allowed_streams_json)
            allowed_senders = json.loads(credential.allowed_senders_json)
        except (SmtpPolicyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PreflightError("dedicated SMTP credential policy is invalid") from exc
        if stream != "SECURITY":
            raise PreflightError("dedicated SMTP credential is not SECURITY-only")
        if allowed_senders != [sender_address]:
            raise PreflightError("dedicated SMTP credential must allow exactly the reviewed sender")

        identity = session.scalar(
            select(SenderIdentity).where(
                SenderIdentity.tenant_id == tenant_id,
                SenderIdentity.email == sender_address,
            )
        )
        if not identity or identity.status != "ACTIVE" or identity.stream.upper() != "SECURITY":
            raise PreflightError("SECURITY sender identity is missing or inactive")

        domain = session.scalar(
            select(ProviderDomain).where(
                ProviderDomain.id == identity.domain_id,
                ProviderDomain.tenant_id == tenant_id,
            )
        )
        if not domain or domain.status in {"SUSPENDED", "REMOVED"}:
            raise PreflightError("SECURITY sender domain is unavailable")

        policy = session.get(TenantMailPolicy, tenant_id)
        if not policy or policy.reputation_state == "SUSPENDED":
            raise PreflightError("tenant mail policy is missing or suspended")

        if live:
            if domain.status != "SENDING_ENABLED" or not domain.sending_enabled:
                raise PreflightError("live SECURITY mode requires a sending-enabled domain")
            if policy.sandbox_mode or policy.sending_disabled:
                raise PreflightError(
                    "live SECURITY mode requires a non-sandbox enabled tenant policy"
                )

        return {
            "status": "pass",
            "mode": mode,
            "security_stream_enabled": enabled,
            "security_live_delivery_enabled": live,
            "security_production_approved": production,
            "tenant_id": tenant_id,
            "credential_id": credential.id,
            "credential_expires_at": expiry.isoformat() if expiry else None,
            "sender": sender_address,
            "domain": domain.domain,
            "domain_status": domain.status,
            "policy_sandbox_mode": policy.sandbox_mode,
            "policy_sending_disabled": policy.sending_disabled,
            "canary_recipients": recipients,
            "canary_max_deliveries": canary_limit,
            "smtp_password_inspected": False,
        }


def main() -> int:
    try:
        result = validate()
    except PreflightError as exc:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "check": "klyrow-security-smtp-runtime",
                    "error": str(exc),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
