"""Unit tests for Middleware command-bound transactional production attestation."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from apps.gateway.app.production_authorization import (
    ProductionAuthorizationError,
    _recipients_sha256,
    provider_payload_from_outbox,
    validate_production_authorization,
)


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
TENANT = "tenant-a"
MESSAGE = "command-00000001"
CORRELATION = "email-correlation-0001"
IDEMPOTENCY = "email-command-key-0001"
SENDER = "sender@example.com"
RECIPIENTS = ["recipient@example.net"]
CATEGORY = "transactional"


def authorization(recipients: list[str] | None = None, **updates) -> dict:
    """Generate a valid command-bound attestation for the fixed test clock."""
    target_recipients = recipients if recipients is not None else RECIPIENTS
    value = {
        "schemaVersion": "1.0",
        "tenantId": TENANT,
        "policyVersion": 2,
        "mode": "TRANSACTIONAL_PRODUCTION",
        "authorizationState": "ACTIVE",
        "killSwitchOpen": True,
        "changeId": "CHG-EMAIL-001",
        "category": CATEGORY,
        "validFrom": (NOW - timedelta(minutes=5)).isoformat(),
        "validUntil": (NOW + timedelta(hours=1)).isoformat(),
        "provider": "klyrow-postal",
        "environment": "production",
        "approvedReleaseSha": "a" * 40,
        "authorizationTimestamp": (NOW - timedelta(minutes=10)).isoformat(),
        "activationTimestamp": (NOW - timedelta(minutes=5)).isoformat(),
        "commandBinding": {
            "messageId": MESSAGE,
            "correlationId": CORRELATION,
            "idempotencyKeySha256": hashlib.sha256(IDEMPOTENCY.encode()).hexdigest(),
            "sender": SENDER,
            "recipientsSha256": _recipients_sha256(target_recipients),
        },
    }
    value.update(updates)
    return value


def validate(
    value: dict, recipients: list[str] | None = None, *, now: datetime = NOW
) -> dict:
    return validate_production_authorization(
        value,
        tenant_id=TENANT,
        message_id=MESSAGE,
        correlation_id=CORRELATION,
        sender=SENDER,
        recipients=recipients if recipients is not None else RECIPIENTS,
        category=CATEGORY,
        idempotency_key=IDEMPOTENCY,
        now=now,
    )


def test_active_transactional_authorization_is_command_bound():
    result = validate(authorization())
    assert result["mode"] == "TRANSACTIONAL_PRODUCTION"
    assert result["commandBinding"]["messageId"] == MESSAGE


@pytest.mark.parametrize(
    "changes",
    [
        {"authorizationState": "REVOKED"},
        {"killSwitchOpen": False},
        {"mode": "CAMPAIGN_PRODUCTION"},
        {"provider": "smtp"},
        {"environment": "staging"},
        {"validUntil": (NOW - timedelta(seconds=1)).isoformat()},
    ],
)
def test_inactive_campaign_expired_or_wrong_provider_authority_fails_closed(changes):
    with pytest.raises(ProductionAuthorizationError):
        validate(authorization(**changes))


def test_command_identity_mismatch_fails_closed():
    value = authorization()
    value["commandBinding"]["messageId"] = "different-command"
    with pytest.raises(ProductionAuthorizationError, match="binding_mismatch"):
        validate(value)


def test_idempotency_binding_mismatch_fails_closed():
    value = authorization()
    value["commandBinding"]["idempotencyKeySha256"] = "b" * 64
    with pytest.raises(ProductionAuthorizationError, match="idempotency_mismatch"):
        validate(value)


def test_outbox_revalidates_and_strips_control_data_before_provider():
    payload = {
        "to": RECIPIENTS,
        "from": SENDER,
        "subject": "test",
        "stream": "transactional",
        "_codestra_production_authorization": authorization(),
    }
    original = deepcopy(payload)
    provider_payload, authorized = provider_payload_from_outbox(
        payload,
        tenant_id=TENANT,
        message_id=MESSAGE,
        correlation_id=CORRELATION,
        now=NOW,
    )
    assert authorized is True
    assert "_codestra_production_authorization" not in provider_payload
    assert "_codestra_production_authorization" in payload
    assert payload == original
    assert provider_payload == {
        key: value for key, value in original.items()
        if key != "_codestra_production_authorization"
    }


def test_recipients_sha256_is_order_and_casing_invariant():
    list_a = ["Alice@Example.com ", "bob@example.net"]
    list_b = ["BOB@EXAMPLE.NET", "alice@example.com"]
    assert _recipients_sha256(list_a) == _recipients_sha256(list_b)


def test_recipient_digest_matches_independent_wire_fixture():
    # Fixed serialized bytes avoid deriving the expected value with the helper.
    expected = hashlib.sha256(b'["alice@example.com","bob@example.net"]').hexdigest()
    assert _recipients_sha256([" BOB@EXAMPLE.NET ", "Alice@Example.com"]) == expected


def test_recipient_order_mismatch_succeeds_with_normalized_sorting():
    middleware_recipients = ["b@example.com", "a@example.com"]
    klyrow_recipients = ["a@example.com", "b@example.com"]
    result = validate(
        authorization(recipients=middleware_recipients), recipients=klyrow_recipients
    )
    assert result["commandBinding"]["recipientsSha256"] == _recipients_sha256(klyrow_recipients)


@pytest.mark.parametrize("recipients", [
    ["different@example.net"], [], RECIPIENTS * 2,
])
def test_changed_recipient_membership_fails_closed(recipients):
    with pytest.raises(ProductionAuthorizationError, match="binding_mismatch"):
        validate(authorization(), recipients=recipients)


@pytest.mark.parametrize("offset", [timedelta(hours=2), timedelta(hours=-4), timedelta(hours=5, minutes=30)])
def test_valid_authorization_with_non_utc_offset_timestamps(offset):
    offset_now = NOW.astimezone(timezone(offset))
    auth_payload = authorization(
        validFrom=(offset_now - timedelta(minutes=5)).isoformat(),
        validUntil=(offset_now + timedelta(hours=1)).isoformat(),
        authorizationTimestamp=(offset_now - timedelta(minutes=10)).isoformat(),
        activationTimestamp=(offset_now - timedelta(minutes=5)).isoformat(),
    )
    result = validate(auth_payload)
    assert result["mode"] == "TRANSACTIONAL_PRODUCTION"


def test_non_utc_execution_clock_matches_same_utc_instant():
    offset_now = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert validate(authorization(), now=offset_now) == validate(authorization())


def test_naive_now_timestamp_fails_closed():
    with pytest.raises(ProductionAuthorizationError, match="clock_invalid"):
        validate(authorization(), now=datetime(2026, 9, 13, 12, 0))


class UndefinedOffset(tzinfo):
    def utcoffset(self, dt):
        return None


def test_execution_clock_with_undefined_offset_fails_closed():
    clock = NOW.replace(tzinfo=UndefinedOffset())
    with pytest.raises(ProductionAuthorizationError, match="clock_invalid"):
        validate(authorization(), now=clock)


@pytest.mark.parametrize("field", ["validFrom", "validUntil"])
def test_naive_window_timestamp_in_payload_fails_closed(field):
    auth_payload = authorization(**{field: "2026-09-13T11:55:00"})
    with pytest.raises(ProductionAuthorizationError, match="window_invalid"):
        validate(auth_payload)


@pytest.mark.parametrize("field", ["authorizationTimestamp", "activationTimestamp"])
def test_naive_audit_timestamp_in_payload_fails_closed(field):
    with pytest.raises(ProductionAuthorizationError, match="timestamps_invalid"):
        validate(authorization(**{field: "2026-09-13T11:50:00"}))


def test_valid_from_boundary_is_inclusive():
    assert validate(authorization(validFrom=NOW.isoformat()))["mode"] == "TRANSACTIONAL_PRODUCTION"


@pytest.mark.parametrize("changes", [
    {"validUntil": NOW.isoformat()},
    {"validFrom": (NOW + timedelta(microseconds=1)).isoformat()},
    {"validFrom": NOW.isoformat(), "validUntil": NOW.isoformat()},
    {"validFrom": (NOW + timedelta(hours=2)).isoformat()},
])
def test_expired_future_empty_or_reversed_window_fails_closed(changes):
    with pytest.raises(ProductionAuthorizationError, match="outside_window"):
        validate(authorization(**changes))


@pytest.mark.parametrize("changes", [
    {"authorizationTimestamp": NOW.isoformat()},
    {"activationTimestamp": (NOW + timedelta(microseconds=1)).isoformat()},
])
def test_reversed_or_future_audit_timestamps_fail_closed(changes):
    with pytest.raises(ProductionAuthorizationError, match="timestamps_invalid"):
        validate(authorization(**changes))


@pytest.mark.parametrize("authority", [
    "invalid", {}, authorization(validUntil=NOW.isoformat()),
])
def test_outbox_rejects_invalid_or_expired_authority_without_mutating_input(authority):
    payload = {
        "to": RECIPIENTS,
        "from": SENDER,
        "subject": "test",
        "stream": "transactional",
        "_codestra_production_authorization": authority,
    }
    original = deepcopy(payload)
    with pytest.raises(ProductionAuthorizationError):
        provider_payload_from_outbox(
            payload, tenant_id=TENANT, message_id=MESSAGE,
            correlation_id=CORRELATION, now=NOW,
        )
    assert payload == original


def test_outbox_without_authority_remains_unauthorized():
    payload = {"to": RECIPIENTS, "from": SENDER, "stream": "transactional"}
    result, authorized = provider_payload_from_outbox(
        payload, tenant_id=TENANT, message_id=MESSAGE,
        correlation_id=CORRELATION, now=NOW,
    )
    assert authorized is False
    assert result == payload
    assert result is not payload
