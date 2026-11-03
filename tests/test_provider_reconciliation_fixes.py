from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("KLYROW_DATABASE_URL", "sqlite:///./test-provider-reconciliation.db")
os.environ.setdefault(
    "KLYROW_SESSION_SECRET",
    "test-secret-provider-reconciliation-minimum-32-characters",
)
os.environ.setdefault("KLYROW_SAFE_MODE", "true")
os.environ.setdefault("KLYROW_ENV", "test")
os.environ.setdefault("KLYROW_SANDBOX_DOMAIN", "klyrow-sink.test")

# Import the composition root before ``provider``. The production composition
# follows this order, and importing the provider first would enter its expected
# back-reference to ``main`` before the provider module has finished loading.
from apps.gateway.app.main import (
    SMTP_EVENT_MAP,
    Base,
    InboundRouteConfig,
    Tenant,
)
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
    middleware_destination_kind,
    middleware_inbound_eligible,
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


def test_legacy_odoo_webhook_allowlist_is_closed() -> None:
    support = SimpleNamespace(
        destination_kind="webhook",
        destination_ref="server-a:odoo-support",
    )
    accounting = SimpleNamespace(
        destination_kind="WEBHOOK",
        destination_ref=" SERVER-A:ODOO-ACCOUNTING ",
    )
    unrelated = SimpleNamespace(
        destination_kind="webhook",
        destination_ref="https://example.invalid/arbitrary-webhook",
    )

    assert middleware_destination_kind(support) == "odoo_helpdesk"
    assert middleware_destination_kind(accounting) == "odoo_accounting"
    assert middleware_inbound_eligible(support, "ACCEPT") is True
    assert middleware_inbound_eligible(accounting, "ACCEPT") is True
    assert middleware_inbound_eligible(support, "QUARANTINE") is False
    assert middleware_destination_kind(unrelated) == "webhook"
    assert middleware_inbound_eligible(unrelated, "ACCEPT") is False


def test_installer_patches_live_provider_globals() -> None:
    install_provider_reconciliation_fixes()
    assert provider.middleware_destination_kind is middleware_destination_kind
    assert provider.middleware_inbound_eligible is middleware_inbound_eligible
    assert provider.reconcile_provider_outbox_dead_letters is reconcile_provider_outbox_dead_letters

    source = Path("apps/gateway/app/runtime_authority_fixes.py").read_text(encoding="utf-8")
    assert "install_provider_reconciliation_fixes" in source
    assert "install_provider_reconciliation_fixes()" in source


def test_legacy_odoo_event_is_replayable_not_skipped(isolated_session) -> None:
    install_provider_reconciliation_fixes()
    current = provider.now()
    route = InboundRouteConfig(
        id="legacy-odoo-route",
        tenant_id="tenant-a",
        address="support@example.com",
        destination_kind="webhook",
        destination_ref="server-a:odoo-support",
        verified=True,
        enabled=True,
    )
    inbound = ProviderInbound(
        id="legacy-inbound",
        tenant_id="tenant-a",
        provider_event_id="legacy-provider-event",
        route_id=route.id,
        message_id_header="<legacy@example.com>",
        sender="sender@example.com",
        recipient="support@example.com",
        subject="Legacy support",
        text_body="body",
        html_body=None,
        attachments_json="[]",
        auth_verdict="PASS",
        spf_result="PASS",
        dkim_result="PASS",
        dmarc_result="PASS",
        arc_result="NONE",
        dmarc_fail_action="ACCEPT",
        disposition="ACCEPT",
        created_at=current,
    )
    event = ProviderEvent(
        id="legacy-event",
        tenant_id="tenant-a",
        message_id=inbound.id,
        kind="inbound.received",
        payload_json='{"event_id":"legacy-event"}',
        state="DEAD_LETTER",
        attempts=8,
        available_at=current,
        last_error="server_a_delivery_failed",
        created_at=current,
        updated_at=current,
    )
    isolated_session.add_all([route, inbound, event])
    isolated_session.flush()

    decision, payload = provider._upgrade_legacy_inbound_event(isolated_session, event)

    assert decision == "requeue"
    assert payload is not None
    assert payload["destination_kind"] == "odoo_helpdesk"
    assert payload["destination_ref"] == "server-a:odoo-support"
    assert payload["event"] == "inbound.received"
    assert payload["attachments"] == []


def test_reconciliation_advances_past_more_than_one_full_blocked_page(
    isolated_session,
) -> None:
    install_provider_reconciliation_fixes()
    current = provider.now()
    old = current - timedelta(days=1)

    blocked = []
    for index in range(55):
        blocked.append(
            ProviderEvent(
                id=f"blocked-{index:03d}",
                tenant_id="tenant-a",
                message_id=f"missing-{index:03d}",
                kind="unknown.event",
                payload_json=json.dumps({"event_id": f"blocked-{index:03d}"}),
                state="DEAD_LETTER",
                attempts=8,
                available_at=old,
                last_error="server_a_delivery_failed",
                created_at=old + timedelta(seconds=index),
                updated_at=old,
            )
        )

    assert "message.delivered" in SMTP_EVENT_MAP
    recoverable_message = ProviderMessage(
        id="recoverable-message",
        tenant_id="tenant-a",
        correlation_id="recoverable-correlation",
        idempotency_key="recoverable-idempotency",
        request_hash="recoverable-request",
        sender="recoverable-sender@example.com",
        recipient="recoverable-recipient@example.com",
        subject="Recoverable lifecycle",
        payload_json="{}",
        stream="TRANSACTIONAL",
        status="DELIVERED",
        sandbox=True,
        provider_message_id="postal-recoverable",
        attempts=1,
        available_at=old,
        created_at=old,
        updated_at=old,
    )
    recoverable = ProviderEvent(
        id="recoverable-event",
        tenant_id="tenant-a",
        message_id=recoverable_message.id,
        kind="message.delivered",
        payload_json='{"event_id":"recoverable-event"}',
        state="DEAD_LETTER",
        attempts=8,
        available_at=old,
        last_error="server_a_delivery_failed",
        created_at=old + timedelta(seconds=56),
        updated_at=old,
    )
    foreign = ProviderEvent(
        id="foreign-blocked",
        tenant_id="tenant-b",
        message_id="foreign-message",
        kind="unknown.event",
        payload_json='{"event_id":"foreign-blocked"}',
        state="DEAD_LETTER",
        attempts=8,
        available_at=old,
        last_error="server_a_delivery_failed",
        created_at=old,
        updated_at=old,
    )
    message = ProviderMessage(
        id="usage-message",
        tenant_id="tenant-a",
        correlation_id="usage-correlation",
        idempotency_key="usage-idempotency",
        request_hash="usage-request",
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
        id="recoverable-usage",
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
    isolated_session.add_all(
        [
            *blocked,
            recoverable_message,
            recoverable,
            foreign,
            message,
            usage,
        ]
    )
    isolated_session.commit()

    first = reconcile_provider_outbox_dead_letters(
        isolated_session,
        tenant_id="tenant-a",
        limit=50,
        apply=True,
    )
    isolated_session.commit()

    assert first["examined"] == 50
    assert first["events_blocked"] == 49
    assert first["usage_requeued"] == 1
    assert first["blocked_events_visible"] == 49
    assert first["unreviewed_events_remaining"] == 7
    assert isolated_session.get(ProviderUsageEvent, usage.id).state == "RETRY"
    assert isolated_session.get(ProviderEvent, recoverable.id).state == "DEAD_LETTER"

    second = reconcile_provider_outbox_dead_letters(
        isolated_session,
        tenant_id="tenant-a",
        limit=50,
        apply=True,
    )
    isolated_session.commit()

    assert second["examined"] == 50
    assert second["events_requeued"] == 1
    assert second["events_blocked"] == 49
    assert second["unreviewed_events_remaining"] == 0
    assert second["blocked_events_visible"] == 55
    assert isolated_session.get(ProviderEvent, recoverable.id).state == "RETRY"
    assert isolated_session.get(ProviderEvent, foreign.id).last_error == (
        "server_a_delivery_failed"
    )
    assert all(
        isolated_session.get(ProviderEvent, item.id).state == "DEAD_LETTER"
        and isolated_session.get(ProviderEvent, item.id).last_error == BLOCKED_RECONCILIATION_MARKER
        for item in blocked
    )


def test_reconciliation_dry_run_does_not_mutate(isolated_session) -> None:
    current = provider.now()
    item = ProviderEvent(
        id="dry-run-event",
        tenant_id="tenant-a",
        message_id="dry-run-message",
        kind="unknown.event",
        payload_json="{}",
        state="DEAD_LETTER",
        attempts=8,
        available_at=current,
        last_error="original_failure",
        created_at=current,
        updated_at=current,
    )
    isolated_session.add(item)
    isolated_session.commit()

    result = reconcile_provider_outbox_dead_letters(
        isolated_session,
        tenant_id="tenant-a",
        limit=1,
        apply=False,
    )

    isolated_session.refresh(item)
    assert result["apply"] is False
    assert result["events_blocked"] == 1
    assert item.state == "DEAD_LETTER"
    assert item.last_error == "original_failure"


@pytest.mark.parametrize("limit", [0, 51])
def test_reconciliation_preserves_fixed_transaction_bound(
    isolated_session,
    limit: int,
) -> None:
    with pytest.raises(
        ValueError,
        match="provider_outbox_reconcile_limit_out_of_range",
    ):
        reconcile_provider_outbox_dead_letters(
            isolated_session,
            tenant_id="tenant-a",
            limit=limit,
            apply=False,
        )
