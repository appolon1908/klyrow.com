from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.gateway.app.main import sha
from apps.gateway.app import operations
from apps.gateway.app import runtime_authority_fixes as fixes


class FakeMessage:
    sender = "alerts@example.invalid"
    to = "sink@example.invalid"
    subject = "authority"
    stream = "transactional"
    campaign_id = None
    reply_to = None
    topic = "transactional"
    callback_metadata = {}
    html = "<p>authority</p>"
    text = "authority"
    headers = {}

    def model_dump(self, *, mode=None):
        return {
            "to": self.to,
            "sender": self.sender,
            "subject": self.subject,
            "stream": self.stream,
            "text": self.text,
            "html": self.html,
        }

    def model_dump_json(self):
        return json.dumps(
            self.model_dump(),
            separators=(",", ":"),
            sort_keys=True,
        )


class MessageLookupSession:
    def __init__(self, persisted_message_id):
        self.persisted_message_id = persisted_message_id
        self.calls = 0

    def scalar(self, _statement):
        self.calls += 1
        return self.persisted_message_id


class CompatibilitySession:
    def __init__(self, legacy, persisted_message_id=None):
        self.legacy = legacy
        self.persisted_message_id = persisted_message_id
        self.calls = 0

    def scalar(self, _statement):
        self.calls += 1
        if self.calls == 1:
            return None
        if self.calls == 2:
            return self.legacy
        if self.calls == 3:
            return self.persisted_message_id
        raise AssertionError("send flow advanced past expected compatibility lookups")


def legacy_record(
    response,
    request_hash="legacy-hash",
    tenant_id="tenant-a",
):
    return SimpleNamespace(
        key="shared-key",
        tenant_id=tenant_id,
        resource_id=response.get("id"),
        response_json=json.dumps(response),
        request_hash=request_hash,
    )


def message_response():
    return {
        "id": "message-a",
        "provider_message_id": "message-a",
        "status": "accepted",
        "safe_mode": True,
        "stream": "transactional",
    }


def test_legacy_message_requires_shape_same_tenant_and_persisted_message():
    response = message_response()
    record = legacy_record(response)
    assert (
        fixes.legacy_message_send_response(
            record,
            MessageLookupSession("message-a"),
            "tenant-a",
        )
        == response
    )
    assert (
        fixes.legacy_message_send_response(
            legacy_record(response, tenant_id="tenant-b"),
            MessageLookupSession("message-a"),
            "tenant-a",
        )
        is None
    )
    assert (
        fixes.legacy_message_send_response(
            record,
            MessageLookupSession(None),
            "tenant-a",
        )
        is None
    )


def test_campaign_shape_is_never_accepted_as_a_legacy_send():
    campaign = {
        "id": "campaign-a",
        "name": "Campaign",
        "status": "draft",
    }
    session = MessageLookupSession("campaign-a")
    assert (
        fixes.legacy_message_send_response(
            legacy_record(campaign),
            session,
            "tenant-a",
        )
        is None
    )
    assert session.calls == 0


def test_genuine_legacy_message_replay_returns_original_response():
    message = FakeMessage()
    expected = message_response()
    session = CompatibilitySession(
        legacy_record(
            expected,
            request_hash=sha(message.model_dump_json()),
        ),
        persisted_message_id="message-a",
    )
    result = asyncio.run(
        fixes.send_with_scoped_legacy_compatibility(
            message,
            {"tenant": "tenant-a", "sub": "caller-a"},
            session,
            "shared-key",
        )
    )
    assert result == expected
    assert session.calls == 3


def test_spoofed_message_shape_without_real_message_does_not_shadow(
    monkeypatch,
):
    class ContinueIntoCurrentFlow(RuntimeError):
        pass

    def stop_after_compatibility(_session, _tenant):
        raise ContinueIntoCurrentFlow

    monkeypatch.setattr(
        operations,
        "enforce_tenant_send_gate",
        stop_after_compatibility,
    )
    session = CompatibilitySession(
        legacy_record(message_response()),
        persisted_message_id=None,
    )
    with pytest.raises(ContinueIntoCurrentFlow):
        asyncio.run(
            fixes.send_with_scoped_legacy_compatibility(
                FakeMessage(),
                {"tenant": "tenant-a", "sub": "caller-a"},
                session,
                "shared-key",
            )
        )
    assert session.calls == 3


def test_non_message_raw_key_does_not_shadow_scoped_message_identity(
    monkeypatch,
):
    class ContinueIntoCurrentFlow(RuntimeError):
        pass

    def stop_after_compatibility(_session, _tenant):
        raise ContinueIntoCurrentFlow

    monkeypatch.setattr(
        operations,
        "enforce_tenant_send_gate",
        stop_after_compatibility,
    )
    campaign = {
        "id": "campaign-a",
        "name": "Campaign",
        "status": "draft",
    }
    session = CompatibilitySession(legacy_record(campaign))
    with pytest.raises(ContinueIntoCurrentFlow):
        asyncio.run(
            fixes.send_with_scoped_legacy_compatibility(
                FakeMessage(),
                {"tenant": "tenant-a", "sub": "caller-a"},
                session,
                "shared-key",
            )
        )
    assert session.calls == 2


def test_changed_genuine_legacy_message_payload_conflicts():
    session = CompatibilitySession(
        legacy_record(
            message_response(),
            request_hash="different",
        ),
        persisted_message_id="message-a",
    )
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(
            fixes.send_with_scoped_legacy_compatibility(
                FakeMessage(),
                {"tenant": "tenant-a", "sub": "caller-a"},
                session,
                "shared-key",
            )
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail == "idempotency_key_payload_mismatch"


def test_operation_response_uses_envelope_correlation_not_storage_digest(
    monkeypatch,
):
    monkeypatch.setattr(
        fixes,
        "_ORIGINAL_OPERATION_JSON",
        lambda _item, _session: {
            "operation_id": "operation-a",
            "correlation_id": "scope:v1:storage-digest",
        },
    )
    item = SimpleNamespace(
        payload_json=json.dumps(
            {
                "envelope": {
                    "correlation_id": "caller-correlation-a",
                }
            }
        )
    )
    result = fixes.operation_json_with_correlation(item, object())
    assert result["correlation_id"] == "caller-correlation-a"


def test_legacy_operation_without_envelope_preserves_original_fallback(
    monkeypatch,
):
    monkeypatch.setattr(
        fixes,
        "_ORIGINAL_OPERATION_JSON",
        lambda _item, _session: {
            "correlation_id": "legacy-raw-key",
        },
    )
    item = SimpleNamespace(payload_json=json.dumps({"payload": {}}))
    assert (
        fixes.operation_json_with_correlation(
            item,
            object(),
        )["correlation_id"]
        == "legacy-raw-key"
    )
