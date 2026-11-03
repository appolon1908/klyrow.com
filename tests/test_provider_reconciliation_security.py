from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault(
    "KLYROW_DATABASE_URL",
    "sqlite:///./test-provider-reconciliation-security.db",
)
os.environ.setdefault(
    "KLYROW_SESSION_SECRET",
    "test-secret-provider-reconciliation-security-minimum-32-characters",
)
os.environ.setdefault("KLYROW_SAFE_MODE", "true")
os.environ.setdefault("KLYROW_ENV", "test")
os.environ.setdefault("KLYROW_SANDBOX_DOMAIN", "klyrow-sink.test")

from apps.gateway.app.main import Base, InboundRouteConfig, Tenant
from apps.gateway.app import provider
from apps.gateway.app.provider import (
    ProviderEvent,
    ProviderInbound,
    ProviderMessage,
    ProviderUsageEvent,
)
from apps.gateway.app.provider_reconciliation_fixes import (
    BLOCKED_RECONCILIATION_MARKER,
    install_provider_reconciliation_fixes,
    reconcile_provider_outbox_dead_letters,
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


def _route(
    *,
    route_id: str,
    tenant_id: str,
    address: str,
) -> InboundRouteConfig:
    return InboundRouteConfig(
        id=route_id,
        tenant_id=tenant_id,
        address=address,
        destination_kind="webhook",
        destination_ref="server-a:odoo-support",
        verified=True,
        enabled=True,
    )


def _inbound(
    *,
    inbound_id: str,
    tenant_id: str,
    route_id: str,
    recipient: str,
    attachments_json: str = "[]",
) -> ProviderInbound:
    return ProviderInbound(
        id=inbound_id,
        tenant_id=tenant_id,
        provider_event_id=f"provider-{inbound_id}",
        route_id=route_id,
        message_id_header=f"<{inbound_id}@example.com>",
        sender=f"sender-{tenant_id}@example.com",
        recipient=recipient,
        subject=f"Subject {tenant_id}",
        text_body=f"Body {tenant_id}",
        html_body=None,
        attachments_json=attachments_json,
        auth_verdict="PASS",
        spf_result="PASS",
        dkim_result="PASS",
        dmarc_result="PASS",
        arc_result="NONE",
        dmarc_fail_action="ACCEPT",
        disposition="ACCEPT",
        created_at=provider.now(),
    )


def _event(
    *,
    event_id: str,
    tenant_id: str,
    message_id: str,
    payload: dict | None = None,
) -> ProviderEvent:
    current = provider.now()
    return ProviderEvent(
        id=event_id,
        tenant_id=tenant_id,
        message_id=message_id,
        kind="inbound.received",
        payload_json=json.dumps(payload or {"event_id": event_id}),
        state="DEAD_LETTER",
        attempts=8,
        available_at=current,
        last_error="server_a_delivery_failed",
        created_at=current,
        updated_at=current,
    )


def test_legacy_upgrade_requires_inbound_and_route_tenant_match(
    isolated_session,
) -> None:
    install_provider_reconciliation_fixes()

    foreign_route = _route(
        route_id="foreign-route",
        tenant_id="tenant-b",
        address="support-b@example.com",
    )
    foreign_inbound = _inbound(
        inbound_id="foreign-inbound",
        tenant_id="tenant-b",
        route_id=foreign_route.id,
        recipient=foreign_route.address,
    )
    event_with_foreign_inbound = _event(
        event_id="event-foreign-inbound",
        tenant_id="tenant-a",
        message_id=foreign_inbound.id,
    )

    mismatched_route = _route(
        route_id="mismatched-route",
        tenant_id="tenant-b",
        address="support-mismatch@example.com",
    )
    local_inbound = _inbound(
        inbound_id="local-inbound",
        tenant_id="tenant-a",
        route_id=mismatched_route.id,
        recipient=mismatched_route.address,
    )
    event_with_foreign_route = _event(
        event_id="event-foreign-route",
        tenant_id="tenant-a",
        message_id=local_inbound.id,
    )

    isolated_session.add_all(
        [
            foreign_route,
            foreign_inbound,
            event_with_foreign_inbound,
            mismatched_route,
            local_inbound,
            event_with_foreign_route,
        ]
    )
    isolated_session.flush()

    for event in (event_with_foreign_inbound, event_with_foreign_route):
        decision, payload = provider._upgrade_legacy_inbound_event(
            isolated_session,
            event,
        )
        assert decision == "blocked"
        assert payload is None


def test_zero_byte_attachment_is_complete_and_replayable(
    isolated_session,
) -> None:
    install_provider_reconciliation_fixes()
    route = _route(
        route_id="empty-attachment-route",
        tenant_id="tenant-a",
        address="support-a@example.com",
    )
    metadata = {
        "filename": "empty.txt",
        "content_type": "text/plain",
        "size": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    inbound = _inbound(
        inbound_id="empty-attachment-inbound",
        tenant_id="tenant-a",
        route_id=route.id,
        recipient=route.address,
        attachments_json=json.dumps([metadata]),
    )
    event = _event(
        event_id="empty-attachment-event",
        tenant_id="tenant-a",
        message_id=inbound.id,
        payload={
            "event_id": "empty-attachment-event",
            "tenant_id": "tenant-a",
            "recipient": inbound.recipient,
            "sender": inbound.sender,
            "subject": inbound.subject,
            "destination_kind": "webhook",
            "destination_ref": "server-a:odoo-support",
            "attachments": [{**metadata, "data_b64": ""}],
        },
    )
    isolated_session.add_all([route, inbound, event])
    isolated_session.flush()

    decision, payload = provider._upgrade_legacy_inbound_event(
        isolated_session,
        event,
    )

    assert decision == "requeue"
    assert payload is not None
    assert payload["tenant_id"] == "tenant-a"
    assert payload["destination_kind"] == "odoo_helpdesk"
    assert payload["attachments"][0]["data_b64"] == ""
    assert payload["attachments"][0]["size"] == 0


def test_one_row_reconciliation_yields_to_usage_after_classifying_blocked_event(
    isolated_session,
) -> None:
    install_provider_reconciliation_fixes()
    old = provider.now() - timedelta(days=1)
    blocked = ProviderEvent(
        id="one-row-blocked",
        tenant_id="tenant-a",
        message_id="missing-inbound",
        kind="unknown.event",
        payload_json="{}",
        state="DEAD_LETTER",
        attempts=8,
        available_at=old,
        last_error="server_a_delivery_failed",
        created_at=old,
        updated_at=old,
    )
    message = ProviderMessage(
        id="one-row-message",
        tenant_id="tenant-a",
        correlation_id="one-row-correlation",
        idempotency_key="one-row-idempotency",
        request_hash="one-row-request",
        sender="sender@example.com",
        recipient="recipient@example.com",
        subject="Usage",
        payload_json="{}",
        stream="TRANSACTIONAL",
        status="DELIVERED",
        sandbox=True,
        attempts=1,
        available_at=old,
        created_at=old,
        updated_at=old,
    )
    usage = ProviderUsageEvent(
        id="one-row-usage",
        tenant_id="tenant-a",
        message_id=message.id,
        stream="TRANSACTIONAL",
        billable_units=1,
        result_category="DELIVERED",
        state="DEAD_LETTER",
        attempts=8,
        available_at=old,
        last_error="server_a_delivery_failed",
        created_at=old,
    )
    isolated_session.add_all([blocked, message, usage])
    isolated_session.commit()

    first = reconcile_provider_outbox_dead_letters(
        isolated_session,
        tenant_id="tenant-a",
        limit=1,
        apply=True,
    )
    isolated_session.commit()

    assert first["examined"] == 1
    assert first["events_blocked"] == 1
    assert first["usage_requeued"] == 0
    assert isolated_session.get(ProviderEvent, blocked.id).last_error == (
        BLOCKED_RECONCILIATION_MARKER
    )
    assert isolated_session.get(ProviderUsageEvent, usage.id).state == ("DEAD_LETTER")

    second = reconcile_provider_outbox_dead_letters(
        isolated_session,
        tenant_id="tenant-a",
        limit=1,
        apply=True,
    )
    isolated_session.commit()

    assert second["examined"] == 1
    assert second["events_blocked"] == 0
    assert second["usage_requeued"] == 1
    assert isolated_session.get(ProviderUsageEvent, usage.id).state == "RETRY"
    assert isolated_session.get(ProviderEvent, blocked.id).state == ("DEAD_LETTER")
