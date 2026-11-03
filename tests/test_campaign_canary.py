from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.gateway.app.main import (
    campaign_canary_payload_allowed,
    campaign_execution_mode,
    campaign_worker_payload_allowed,
    enforce_campaign_canary,
)


def configured(monkeypatch):
    values = {
        "KLYROW_ENV": "production",
        "KLYROW_CAMPAIGN_EXECUTION_MODE": "CAMPAIGN_CANARY_ONLY",
        "KLYROW_CAMPAIGN_CANARY_ENABLED": "true",
        "KLYROW_CAMPAIGN_CANARY_MAX_RECIPIENTS": "1",
        "KLYROW_CAMPAIGN_CANARY_TENANT_ID": "tenant-approved",
        "KLYROW_CAMPAIGN_CANARY_CAMPAIGN_ID": "campaign-approved",
        "KLYROW_CAMPAIGN_CANARY_RECIPIENTS": "recipient@klyrow-sink.test",
        "KLYROW_CAMPAIGN_CANARY_SENDER": "sender@mail.klyrow.com",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return SimpleNamespace(
        to="recipient@klyrow-sink.test",
        sender="sender@mail.klyrow.com",
        campaign_id="campaign-approved",
    )


def denied(call, code):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code in {403, 503}
    assert exc.value.detail == code


def test_campaign_canary_only_accepts_exact_bounded_scope(monkeypatch):
    request = configured(monkeypatch)
    enforce_campaign_canary(request, {"tenant": "tenant-approved"}, None)
    assert campaign_execution_mode() == "CAMPAIGN_CANARY_ONLY"


def test_campaign_canary_defaults_closed_and_rejects_invalid_mode(monkeypatch):
    monkeypatch.delenv("KLYROW_CAMPAIGN_EXECUTION_MODE", raising=False)
    assert campaign_execution_mode() == "CAMPAIGN_EXECUTION_DISABLED"
    monkeypatch.setenv("KLYROW_CAMPAIGN_EXECUTION_MODE", "UNRESTRICTED")
    assert campaign_execution_mode() == "CAMPAIGN_EXECUTION_DISABLED"


def test_campaign_canary_negative_scope_controls(monkeypatch):
    request = configured(monkeypatch)
    denied(
        lambda: enforce_campaign_canary(request, {"tenant": "tenant-other"}, None),
        "campaign_canary_tenant_denied",
    )
    request = configured(monkeypatch)
    request.campaign_id = "campaign-other"
    denied(
        lambda: enforce_campaign_canary(request, {"tenant": "tenant-approved"}, None),
        "campaign_canary_campaign_denied",
    )
    request = configured(monkeypatch)
    request.to = "arbitrary@example.net"
    denied(
        lambda: enforce_campaign_canary(request, {"tenant": "tenant-approved"}, None),
        "campaign_canary_recipient_denied",
    )


def test_campaign_canary_hard_limit_and_allowlist_are_server_controlled(monkeypatch):
    request = configured(monkeypatch)
    monkeypatch.setenv("KLYROW_CAMPAIGN_CANARY_MAX_RECIPIENTS", "2")
    denied(
        lambda: enforce_campaign_canary(request, {"tenant": "tenant-approved"}, None),
        "campaign_canary_hard_limit_invalid",
    )
    request = configured(monkeypatch)
    monkeypatch.setenv(
        "KLYROW_CAMPAIGN_CANARY_RECIPIENTS", "recipient@klyrow-sink.test,second@klyrow-sink.test"
    )
    denied(
        lambda: enforce_campaign_canary(request, {"tenant": "tenant-approved"}, None),
        "campaign_canary_recipient_denied",
    )


def test_worker_revalidates_exact_campaign_canary_scope(monkeypatch):
    configured(monkeypatch)
    payload = {
        "to": ["recipient@klyrow-sink.test"],
        "from": "sender@mail.klyrow.com",
        "campaign_id": "campaign-approved",
        "stream": "marketing",
    }
    assert campaign_canary_payload_allowed(payload, "tenant-approved")
    assert not campaign_canary_payload_allowed(
        {**payload, "to": ["arbitrary@example.net"]}, "tenant-approved"
    )
    assert not campaign_canary_payload_allowed({**payload, "stream": "bulk"}, "tenant-approved")
    assert not campaign_canary_payload_allowed(payload, "tenant-other")


def test_explicit_production_mode_is_distinct_from_canary(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setenv("KLYROW_CAMPAIGN_EXECUTION_MODE", "CAMPAIGN_PRODUCTION_ENABLED")
    payload = {
        "to": ["customer@example.net"],
        "from": "sender@mail.klyrow.com",
        "campaign_id": "campaign-production",
        "stream": "marketing",
    }
    assert campaign_worker_payload_allowed(payload, "tenant-production")
    assert not campaign_worker_payload_allowed({**payload, "stream": "bulk"}, "tenant-production")
