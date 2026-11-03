"""Canonical, fail-closed authorization guards for every mail ingress path.

This module intentionally keeps model imports inside functions.  The gateway,
provider API, and SMTP relay all depend on it, while their SQLAlchemy models
are registered by those modules during application startup.
"""

import json
import os
from datetime import UTC

from fastapi import HTTPException
from sqlalchemy import func, select

HARD_SUPPRESSION_REASONS = {
    "hard_bounce",
    "complaint",
    "bounced",
    "complained",
    "invalid",
    "abuse",
    "policy",
}


def stream_priority(stream: str) -> int:
    """Lower values are claimed first; bulk/marketing can never starve security."""

    return {
        "SECURITY": 0,
        "SYSTEM": 10,
        "TRANSACTIONAL": 20,
        "MARKETING": 80,
        "BULK": 100,
    }.get(stream.upper(), 1000)


def suppression_record(s, tenant_id: str, email: str):
    """Return the tenant-scoped global suppression record, if any."""

    from .main import Suppression

    return s.scalar(
        select(Suppression).where(
            Suppression.tenant_id == tenant_id,
            Suppression.email == email.lower(),
        )
    )


def enforce_suppression(
    s,
    tenant_id: str,
    email: str,
    stream: str,
    campaign_id: str | None = None,
) -> None:
    """Apply purpose-scoped suppression consistently on all send paths."""

    from .preferences import ScopedSuppression

    normalized_stream = stream.lower()
    normalized_email = email.lower()
    item = suppression_record(s, tenant_id, normalized_email)
    if item and (item.reason in HARD_SUPPRESSION_REASONS or normalized_stream == "marketing"):
        raise HTTPException(422, "recipient_suppressed")
    if normalized_stream == "marketing" and campaign_id:
        scoped = s.scalar(
            select(ScopedSuppression.id).where(
                ScopedSuppression.tenant_id == tenant_id,
                ScopedSuppression.email == normalized_email,
                ScopedSuppression.scope == "LIST",
                ScopedSuppression.scope_id == campaign_id,
            )
        )
        if scoped:
            raise HTTPException(422, "recipient_suppressed")


def enforce_consent(
    s,
    tenant_id: str,
    email: str,
    stream: str,
    topic: str = "marketing",
) -> None:
    """Require stored consent; caller-supplied consent claims are never trusted."""

    if stream.lower() != "marketing":
        return
    from .saas import Consent, Preference, Profile

    profile = s.scalar(
        select(Profile).where(
            Profile.tenant_id == tenant_id,
            Profile.email == email.lower(),
        )
    )
    if not profile:
        raise HTTPException(422, "marketing_consent_required")
    preference = s.scalar(
        select(Preference).where(
            Preference.tenant_id == tenant_id,
            Preference.profile_id == profile.id,
            Preference.topic == topic,
        )
    )
    latest = s.scalar(
        select(Consent)
        .where(
            Consent.tenant_id == tenant_id,
            Consent.profile_id == profile.id,
            Consent.topic == topic,
        )
        .order_by(Consent.occurred_at.desc())
    )
    if not preference or not preference.subscribed or not latest or latest.status != "granted":
        raise HTTPException(422, "marketing_consent_required")


def _enforce_sandbox_recipient(s, tenant_id: str, recipient: str, sandbox: bool) -> None:
    if not sandbox:
        return
    from .provider import TenantMailPolicy

    policy = s.get(TenantMailPolicy, tenant_id)
    # Legacy tenants without a provider policy retain their existing behavior.
    # Once a policy exists, its allowlist is authoritative and fail-closed.
    if not policy:
        return
    try:
        allowed = {
            str(value).lower() for value in json.loads(policy.allowed_test_recipients_json or "[]")
        }
    except (TypeError, ValueError):
        raise HTTPException(503, "sandbox_recipient_policy_invalid")
    sink_domain = os.getenv("KLYROW_SANDBOX_DOMAIN", "klyrow-sink.test").lower()
    normalized = recipient.lower()
    if normalized not in allowed and not normalized.endswith("@" + sink_domain):
        raise HTTPException(403, "sandbox_recipient_not_allowed")


def _enforce_warmup_quota(s, tenant, policy) -> None:
    if not policy:
        return
    from datetime import datetime, timedelta

    from .main import Message
    from .provider import ProviderMessage

    current = datetime.now(UTC)
    hour_ago = current - timedelta(hours=1)
    day_ago = current - timedelta(days=1)
    hourly = (
        s.scalar(
            select(func.count(Message.id)).where(
                Message.tenant_id == tenant.id, Message.created_at >= hour_ago
            )
        )
        or 0
    ) + (
        s.scalar(
            select(func.count(ProviderMessage.id)).where(
                ProviderMessage.tenant_id == tenant.id, ProviderMessage.created_at >= hour_ago
            )
        )
        or 0
    )
    daily = (
        s.scalar(
            select(func.count(Message.id)).where(
                Message.tenant_id == tenant.id, Message.created_at >= day_ago
            )
        )
        or 0
    ) + (
        s.scalar(
            select(func.count(ProviderMessage.id)).where(
                ProviderMessage.tenant_id == tenant.id, ProviderMessage.created_at >= day_ago
            )
        )
        or 0
    )
    hourly_limit = min(policy.hourly_limit, policy.warmup_hourly_limit)
    daily_limit = min(policy.daily_limit, policy.warmup_daily_limit, tenant.quota)
    if policy.reputation_state == "LIMITED":
        hourly_limit = max(1, hourly_limit // 2)
        daily_limit = max(1, daily_limit // 2)
    if hourly >= hourly_limit or daily >= daily_limit:
        raise HTTPException(429, "provider_quota_exceeded")


def authorize_send(
    s,
    *,
    tenant_id: str,
    sender: str,
    recipient: str,
    stream: str,
    sandbox: bool,
    campaign_id: str | None = None,
    topic: str = "marketing",
) -> dict:
    """Run the common guard chain before route-specific persistence."""

    from .main import Tenant
    from .provider import TenantMailPolicy

    if os.getenv("KLYROW_PLATFORM_EMERGENCY_STOP", "false").lower() == "true":
        raise HTTPException(503, "platform_emergency_stop")
    tenant = s.get(Tenant, tenant_id)
    if not tenant or not tenant.enabled:
        raise HTTPException(403, "tenant_suspended")
    normalized_stream = stream.lower()
    if normalized_stream not in {"transactional", "security", "system", "marketing", "bulk"}:
        raise HTTPException(422, "invalid_message_stream")
    normalized_sender = sender.lower()
    normalized_recipient = recipient.lower()
    enforce_suppression(s, tenant_id, normalized_recipient, normalized_stream, campaign_id)
    enforce_consent(s, tenant_id, normalized_recipient, normalized_stream, topic)
    _enforce_sandbox_recipient(s, tenant_id, normalized_recipient, sandbox)
    _enforce_warmup_quota(s, tenant, s.get(TenantMailPolicy, tenant_id))
    return {
        "tenant": tenant,
        "sender": normalized_sender,
        "recipient": normalized_recipient,
        "stream": normalized_stream,
        "sandbox": sandbox,
    }


def billing_identity(s, tenant_id: str, *, sandbox: bool) -> tuple[str, str]:
    """Resolve the canonical billing identity before accepting a message."""

    from .billing import BillingSubscription

    subscription = s.scalar(
        select(BillingSubscription).where(BillingSubscription.tenant_id == tenant_id)
    )
    if subscription and subscription.status in {"TRIALING", "ACTIVE", "PAST_DUE", "GRACE_PERIOD"}:
        return subscription.id, subscription.price_id
    if sandbox:
        return "sandbox", "sandbox"
    raise HTTPException(402, "subscription_not_billable")
