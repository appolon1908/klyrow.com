from pathlib import Path
from datetime import datetime, timezone

from openapi_spec_validator import validate

from apps.gateway.app.main import MiddlewareCommandOperation, app
from apps.gateway.app.operations import IntegrationOutbox
from apps.gateway.app.production_api import (
    ContactPatch,
    CampaignDispatchConfiguration,
    CampaignPatch,
    CampaignSchedule,
    MauticCommand,
    SuppressionIn,
    _authorize_operation_read,
    _authorize_operation_mutation,
    _require_permission,
    contact_delete,
    contact_patch,
    campaign_cancel,
    campaign_dispatch_configuration,
    campaign_patch,
    campaign_schedule,
    message_cancel,
    mautic_command,
    postal_health,
    suppression_create,
    suppression_delete,
    system_readiness,
)
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError


def test_required_production_routes_are_documented():
    paths = app.openapi()["paths"]
    required = {
        ("get", "/health/live"),
        ("get", "/health/ready"),
        ("get", "/v1/me/permissions"),
        ("get", "/v1/me/capabilities"),
        ("get", "/v1/me/sessions"),
        ("get", "/v1/organizations/{organization_id}"),
        ("get", "/v1/organizations/{organization_id}/members"),
        ("post", "/v1/organizations/{organization_id}/members"),
        ("patch", "/v1/organizations/{organization_id}/members/{member_id}"),
        ("get", "/v1/domains/{domain_id}"),
        ("patch", "/v1/domains/{domain_id}"),
        ("delete", "/v1/domains/{domain_id}"),
        ("get", "/v1/domains/{domain_id}/dns"),
        ("get", "/v1/domains/{domain_id}/verification"),
        ("post", "/v1/messages/{message_id}/cancel"),
        ("get", "/v1/templates/{template_id}"),
        ("patch", "/v1/templates/{template_id}"),
        ("delete", "/v1/templates/{template_id}"),
        ("get", "/v1/contacts/{contact_id}"),
        ("patch", "/v1/contacts/{contact_id}"),
        ("delete", "/v1/contacts/{contact_id}"),
        ("get", "/v1/lists"),
        ("post", "/v1/lists"),
        ("get", "/v1/lists/{list_id}"),
        ("patch", "/v1/lists/{list_id}"),
        ("delete", "/v1/lists/{list_id}"),
        ("patch", "/v1/campaigns/{campaign_id}"),
        ("put", "/v1/campaigns/{campaign_id}/dispatch-configuration"),
        ("post", "/v1/campaigns/{campaign_id}/schedule"),
        ("post", "/v1/campaigns/{campaign_id}/cancel"),
        ("get", "/v1/tracking/events"),
        ("get", "/v1/tracking/events/{event_id}"),
        ("get", "/v1/tracking/messages/{message_id}"),
        ("post", "/v1/suppressions"),
        ("delete", "/v1/suppressions/{suppression_id}"),
        ("get", "/v1/bounces"),
        ("get", "/v1/complaints"),
        ("get", "/v1/billing/account"),
        ("get", "/v1/billing/plans"),
        ("get", "/v1/operations"),
        ("get", "/v1/operations/{operation_id}/events"),
        ("get", "/v1/operations/{operation_id}/attempts"),
        ("post", "/v1/operations/{operation_id}/cancel"),
        ("post", "/v1/operations/{operation_id}/reconcile"),
        ("get", "/v1/providers/postal/health"),
        ("get", "/v1/providers/postal/status"),
        ("post", "/v1/integrations/mautic/commands"),
        ("get", "/v1/integrations/mautic/operations"),
        ("get", "/v1/integrations/mautic/operations/{operation_id}"),
        ("post", "/v1/integrations/mautic/operations/{operation_id}/reconcile"),
        ("get", "/v1/system/capabilities"),
        ("get", "/v1/system/readiness"),
        ("get", "/health"),
        ("get", "/readiness"),
        ("get", "/dependencies"),
        ("get", "/capabilities"),
        ("post", "/v1/webhooks/postal-inbound"),
    }
    missing = sorted(
        f"{method.upper()} {path}"
        for method, path in required
        if method not in paths.get(path, {})
    )
    assert missing == []


def test_canonical_openapi_is_structurally_valid():
    validate(app.openapi())


def test_established_canonical_aliases_remain_documented():
    paths = app.openapi()["paths"]
    for method, path in {
        ("post", "/v1/messages"),
        ("get", "/v1/messages"),
        ("get", "/v1/messages/{mid}"),
        ("get", "/v1/email/{mid}/events"),
        ("post", "/v1/auth/logout"),
        ("get", "/v1/billing/usage"),
        ("get", "/v1/billing/invoices"),
    }:
        assert method in paths[path]


def test_production_migration_is_required_by_the_model():
    migration = Path("migrations/2026090201_full_platform_api.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS contact_lists" in migration
    assert "integration_outbox_state_known" in migration
    attachments = Path("migrations/2026090202_webmail_attachments.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS webmail_attachments" in attachments
    assert "octet_length(content) = size" in attachments
    result_authority = Path(
        "migrations/2026090203_integration_result_authority.sql"
    ).read_text()
    assert "uq_integration_result_source_key" in result_authority
    assert "UNIQUE (tenant_id, source, result_key)" in result_authority
    storage = Path(
        "migrations/2026090204_webmail_inbound_storage_authority.sql"
    ).read_text()
    assert "storage_used_bytes" in storage
    assert (
        "DROP CONSTRAINT IF EXISTS uq_webmail_attachment_message_digest_name" in storage
    )
    outcomes = Path(
        "migrations/2026090205_middleware_command_outcome_authority.sql"
    ).read_text()
    assert "unknown_outcome" in outcomes
    assert "processing" in outcomes and "submitted" in outcomes
    outbox_authority = Path(
        "migrations/2026090206_email_outbox_operation_authority.sql"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS operation_id" in outbox_authority
    assert "ADD COLUMN IF NOT EXISTS correlation_id" in outbox_authority


def test_mautic_result_visibility_requires_command_specific_permission():
    item = IntegrationOutbox(
        id="mautic-result",
        tenant_id="tenant-a",
        target="MAUTIC",
        event_type="contact.upsert.v1",
        aggregate_id="contact-1",
        payload_json="{}",
        idempotency_key="mautic-result-key",
    )
    with pytest.raises(HTTPException) as denied:
        _authorize_operation_read(
            {"role": "ANALYST", "permissions": ["analytics.read"]},
            item,
        )
    assert denied.value.status_code == 403
    _authorize_operation_read(
        {"role": "DEVELOPER", "permissions": ["contact.manage"]},
        item,
    )


def test_contact_mutations_require_contact_management_permission():
    context = {
        "tenant": "tenant-a",
        "role": "ANALYST",
        "permissions": ["analytics.read"],
    }
    with pytest.raises(HTTPException) as denied:
        contact_patch("contact-a", ContactPatch(name="Denied"), context, None)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied_delete:
        contact_delete("contact-a", context, None)
    assert denied_delete.value.status_code == 403
    _require_permission(
        {"role": "MARKETING", "permissions": ["contact.manage"]},
        "contact.manage",
    )


def test_message_cancel_requires_send_permission_before_state_access():
    with pytest.raises(HTTPException) as denied:
        message_cancel(
            "message-a",
            {"tenant": "tenant-a", "role": "READ_ONLY", "permissions": ["mail.read"]},
            None,
        )
    assert denied.value.status_code == 403


def test_suppression_mutations_require_contact_management_permission():
    context = {
        "tenant": "tenant-a",
        "role": "ANALYST",
        "permissions": ["analytics.read"],
    }
    with pytest.raises(HTTPException) as denied_create:
        suppression_create(
            SuppressionIn(email="blocked@example.com", reason="policy"),
            context,
            None,
        )
    assert denied_create.value.status_code == 403
    with pytest.raises(HTTPException) as denied_delete:
        suppression_delete("suppression-a", context, None)
    assert denied_delete.value.status_code == 403


def test_campaign_mutations_require_campaign_management_permission():
    context = {
        "tenant": "tenant-a",
        "role": "ANALYST",
        "permissions": ["analytics.read"],
    }
    with pytest.raises(HTTPException) as patch_denied:
        campaign_patch("campaign-a", CampaignPatch(name="Denied"), context, None)
    assert patch_denied.value.status_code == 403
    with pytest.raises(HTTPException) as configuration_denied:
        campaign_dispatch_configuration(
            "campaign-a",
            CampaignDispatchConfiguration(
                sender_id="sender-a", template_id="template-a"
            ),
            context,
            None,
        )
    assert configuration_denied.value.status_code == 403
    with pytest.raises(HTTPException) as schedule_denied:
        campaign_schedule(
            "campaign-a",
            CampaignSchedule(scheduled_at=datetime.now(timezone.utc)),
            context,
            None,
        )
    assert schedule_denied.value.status_code == 403
    with pytest.raises(HTTPException) as cancel_denied:
        campaign_cancel("campaign-a", context, None)
    assert cancel_denied.value.status_code == 403


def test_every_operation_mutation_requires_specific_write_authority():
    read_only = {"role": "READ_ONLY", "permissions": ["mail.read"]}
    command = MiddlewareCommandOperation(command="email.message.cancel.v1")
    with pytest.raises(HTTPException) as command_denied:
        _authorize_operation_mutation(read_only, command)
    assert command_denied.value.status_code == 403
    for target, event_type in (
        ("N8N", "MailDeliveryStatusV1"),
        ("ODOO", "KlyrowBillingSyncV1"),
        ("UNKNOWN", "UnknownV1"),
    ):
        operation = IntegrationOutbox(target=target, event_type=event_type)
        with pytest.raises(HTTPException) as denied:
            _authorize_operation_mutation(read_only, operation)
        assert denied.value.status_code == 403
    _authorize_operation_mutation(
        {"role": "SERVICE", "permissions": ["klyrow.middleware.operation.write"]},
        command,
    )


def test_postal_health_counts_are_tenant_scoped(monkeypatch):
    class RecordingSession:
        def __init__(self):
            self.statements = []

        def scalar(self, statement):
            self.statements.append(str(statement))
            return 0

    monkeypatch.delenv("KLYROW_POSTAL_API_URL", raising=False)
    session = RecordingSession()
    postal_health({"tenant": "tenant-a"}, session)
    assert len(session.statements) == 2
    assert all(
        "email_outbox.tenant_id" in statement for statement in session.statements
    )


def test_system_readiness_passes_authenticated_tenant_to_postal(monkeypatch):
    observed = {}

    class ReadinessSession:
        def execute(self, _statement):
            return None

    def fake_postal(context, _session):
        observed.update(context)
        return {"configured": True, "status": "ok"}

    monkeypatch.setattr("apps.gateway.app.production_api.postal_health", fake_postal)
    response = system_readiness(
        {"tenant": "tenant-a", "sub": "operator"}, ReadinessSession()
    )
    assert observed["tenant"] == "tenant-a"
    assert response["status"] == "ready"


def test_concurrent_mautic_idempotency_insert_returns_durable_winner():
    class ConcurrentSession:
        def __init__(self):
            self.scalar_calls = 0
            self.winner = None
            self.rolled_back = False

        def scalar(self, statement):
            del statement
            self.scalar_calls += 1
            return None if self.scalar_calls == 1 else self.winner

        def add(self, item):
            if isinstance(item, IntegrationOutbox):
                item.state = item.state or "PENDING"
                item.created_at = item.created_at or datetime.now(timezone.utc)
                item.updated_at = item.updated_at or datetime.now(timezone.utc)
                self.winner = item

        def commit(self):
            raise IntegrityError(
                "INSERT integration_outbox", {}, RuntimeError("concurrent unique key")
            )

        def rollback(self):
            self.rolled_back = True

    session = ConcurrentSession()
    response = mautic_command(
        MauticCommand(
            command="contact.upsert.v1",
            aggregate_id="contact-1",
            payload={"email": "safe@example.test"},
            request_id="request-0001",
            timestamp=datetime.now(timezone.utc),
        ),
        {"tenant": "tenant-a", "sub": "operator", "role": "platform_admin"},
        session,
        "mautic-idempotency-0001",
        "mautic-correlation-0001",
    )
    assert response["operation_id"] == session.winner.id
    assert response["status"] == "QUEUED"
    assert session.rolled_back is True
