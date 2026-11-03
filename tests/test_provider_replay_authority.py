from __future__ import annotations

import hashlib
import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault(
    "KLYROW_DATABASE_URL",
    "sqlite:///./test-provider-replay-authority.db",
)
os.environ.setdefault(
    "KLYROW_SESSION_SECRET",
    "test-secret-provider-replay-authority-minimum-32-characters",
)
os.environ.setdefault("KLYROW_SAFE_MODE", "true")
os.environ.setdefault("KLYROW_ENV", "test")
os.environ.setdefault("KLYROW_SANDBOX_DOMAIN", "klyrow-sink.test")

from apps.gateway.app.main import (
    Base,
    EmailOutbox,
    InboundRouteConfig,
    Message,
    Tenant,
)
from apps.gateway.app import provider
from apps.gateway.app.provider import (
    ProviderEvent,
    ProviderInbound,
    ProviderMessage,
)
from apps.gateway.app.provider_reconciliation_fixes import (
    _upgrade_legacy_inbound_event,
    _upgrade_lifecycle_event,
)


@pytest.fixture
def isolated_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        session.add_all(
            [
                Tenant(id="tenant-a", name="A", quota=100),
                Tenant(id="tenant-b", name="B", quota=100),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def _route() -> InboundRouteConfig:
    return InboundRouteConfig(
        id="route-a",
        tenant_id="tenant-a",
        address="support-a@example.com",
        destination_kind="webhook",
        destination_ref="server-a:odoo-support",
        verified=True,
        enabled=True,
    )


def _inbound(manifest: list[dict]) -> ProviderInbound:
    return ProviderInbound(
        id="inbound-a",
        tenant_id="tenant-a",
        provider_event_id="provider-inbound-a",
        route_id="route-a",
        message_id_header="<inbound-a@example.com>",
        sender="sender-a@example.com",
        recipient="support-a@example.com",
        subject="Trusted subject",
        text_body="Trusted body",
        html_body=None,
        attachments_json=json.dumps(manifest),
        auth_verdict="PASS",
        spf_result="PASS",
        dkim_result="PASS",
        dmarc_result="PASS",
        arc_result="NONE",
        dmarc_fail_action="ACCEPT",
        disposition="ACCEPT",
    )


def _inbound_event(attachments: list[dict]) -> ProviderEvent:
    return ProviderEvent(
        id="event-inbound-a",
        tenant_id="tenant-a",
        message_id="inbound-a",
        kind="inbound.received",
        payload_json=json.dumps(
            {
                "event_id": "event-inbound-a",
                "tenant_id": "tenant-a",
                "recipient": "support-a@example.com",
                "sender": "sender-a@example.com",
                "subject": "Trusted subject",
                "destination_kind": "webhook",
                "attachments": attachments,
            }
        ),
        state="DEAD_LETTER",
        attempts=8,
        last_error="server_a_delivery_failed",
    )


def test_replay_requires_every_authoritative_attachment(
    isolated_session,
) -> None:
    trusted = b"trusted"
    manifest = [
        {
            "filename": "evidence.txt",
            "content_type": "text/plain",
            "size": len(trusted),
            "sha256": hashlib.sha256(trusted).hexdigest(),
        }
    ]
    isolated_session.add_all([_route(), _inbound(manifest)])
    isolated_session.flush()

    missing = _inbound_event([])
    isolated_session.add(missing)
    isolated_session.flush()

    decision, payload = _upgrade_legacy_inbound_event(
        isolated_session,
        missing,
    )
    assert decision == "blocked"
    assert payload is None


def test_replay_rejects_self_consistent_replacement_bytes(
    isolated_session,
) -> None:
    trusted = b"trusted"
    replacement = b"replace"
    manifest = [
        {
            "filename": "evidence.txt",
            "content_type": "text/plain",
            "size": len(trusted),
            "sha256": hashlib.sha256(trusted).hexdigest(),
        }
    ]
    supplied = [
        {
            "filename": "evidence.txt",
            "content_type": "text/plain",
            "size": len(replacement),
            "sha256": hashlib.sha256(replacement).hexdigest(),
            "data_b64": provider.base64.b64encode(replacement).decode("ascii"),
        }
    ]
    isolated_session.add_all([_route(), _inbound(manifest)])
    isolated_session.flush()

    event = _inbound_event(supplied)
    isolated_session.add(event)
    isolated_session.flush()

    decision, payload = _upgrade_legacy_inbound_event(
        isolated_session,
        event,
    )
    assert decision == "blocked"
    assert payload is None


def test_replay_normalizes_verified_bytes_to_relational_manifest(
    isolated_session,
) -> None:
    trusted = b"trusted"
    manifest = [
        {
            "filename": "evidence.txt",
            "content_type": "text/plain",
            "size": len(trusted),
            "sha256": hashlib.sha256(trusted).hexdigest(),
        }
    ]
    supplied = [
        {
            **manifest[0],
            "data_b64": provider.base64.b64encode(trusted).decode("ascii"),
            "untrusted_extra": "removed",
        }
    ]
    isolated_session.add_all([_route(), _inbound(manifest)])
    isolated_session.flush()

    event = _inbound_event(supplied)
    isolated_session.add(event)
    isolated_session.flush()

    decision, payload = _upgrade_legacy_inbound_event(
        isolated_session,
        event,
    )
    assert decision == "requeue"
    assert payload is not None
    assert payload["attachments"] == [
        {
            **manifest[0],
            "data_b64": provider.base64.b64encode(trusted).decode("ascii"),
        }
    ]


def _provider_message() -> ProviderMessage:
    return ProviderMessage(
        id="provider-message-a",
        tenant_id="tenant-a",
        correlation_id="correlation-a",
        idempotency_key="idempotency-a",
        request_hash="request-a",
        sender="sender-a@example.com",
        recipient="recipient-a@example.com",
        subject="Provider message",
        payload_json="{}",
        stream="TRANSACTIONAL",
        status="DELIVERED",
        sandbox=True,
        provider_message_id="postal-a",
    )


def test_lifecycle_rebuild_overwrites_cross_tenant_payload_identity(
    isolated_session,
) -> None:
    message = _provider_message()
    event = ProviderEvent(
        id="lifecycle-a",
        tenant_id="tenant-a",
        message_id=message.id,
        kind="message.delivered",
        payload_json=json.dumps(
            {
                "event_id": "attacker-event",
                "tenant_id": "tenant-b",
                "customer_id": "tenant-b",
                "message_id": "foreign-message",
                "correlation_id": "foreign-correlation",
                "sender": "sender-b@example.com",
                "recipient": "recipient-b@example.com",
                "provider_message_id": "postal-b",
                "metadata": {"tenant_id": "tenant-b", "secret": "drop-me"},
            }
        ),
        state="DEAD_LETTER",
        attempts=8,
        last_error="server_a_delivery_failed",
    )
    isolated_session.add_all([message, event])
    isolated_session.flush()

    decision, payload = _upgrade_lifecycle_event(isolated_session, event)

    assert decision == "requeue"
    assert payload is not None
    assert payload["event_id"] == event.id
    assert payload["tenant_id"] == "tenant-a"
    assert payload["customer_id"] == "tenant-a"
    assert payload["message_id"] == message.id
    assert payload["correlation_id"] == message.correlation_id
    assert payload["provider_message_id"] == message.provider_message_id
    assert payload["sender"] == message.sender
    assert payload["recipient"] == message.recipient
    assert payload["metadata"] == {}


def test_lifecycle_without_tenant_owned_message_stays_blocked(
    isolated_session,
) -> None:
    event = ProviderEvent(
        id="lifecycle-missing",
        tenant_id="tenant-a",
        message_id="missing-message",
        kind="email.delivered",
        payload_json=json.dumps(
            {
                "tenant_id": "tenant-a",
                "message_id": "missing-message",
            }
        ),
        state="DEAD_LETTER",
        attempts=8,
        last_error="server_a_delivery_failed",
    )
    isolated_session.add(event)
    isolated_session.flush()

    decision, payload = _upgrade_lifecycle_event(isolated_session, event)

    assert decision == "blocked"
    assert payload is None


def test_core_lifecycle_uses_tenant_scoped_outbox_correlation(
    isolated_session,
) -> None:
    message = Message(
        id="core-message-a",
        tenant_id="tenant-a",
        recipient="recipient-a@example.com",
        sender="sender-a@example.com",
        subject="Core message",
        status="submitted",
    )
    outbox = EmailOutbox(
        id="outbox-a",
        tenant_id="tenant-a",
        message_id=message.id,
        operation_id="operation-a",
        correlation_id="correlation-a",
        provider_message_id="postal-core-a",
        payload="{}",
        state="failed",
    )
    event = ProviderEvent(
        id="core-lifecycle-a",
        tenant_id="tenant-a",
        message_id=message.id,
        kind="email.submitted",
        payload_json=json.dumps(
            {
                "event_id": "old-event",
                "stream": "transactional",
                "metadata": {"provider_event": "MessageSent"},
            }
        ),
        state="DEAD_LETTER",
        attempts=8,
        last_error="server_a_delivery_failed",
    )
    isolated_session.add_all([message, outbox, event])
    isolated_session.flush()

    decision, payload = _upgrade_lifecycle_event(isolated_session, event)

    assert decision == "requeue"
    assert payload is not None
    assert payload["tenant_id"] == "tenant-a"
    assert payload["message_id"] == message.id
    assert payload["operation_id"] == outbox.operation_id
    assert payload["correlation_id"] == outbox.correlation_id
    assert payload["provider_message_id"] == outbox.provider_message_id
    assert payload["metadata"] == {"provider_event": "MessageSent"}
