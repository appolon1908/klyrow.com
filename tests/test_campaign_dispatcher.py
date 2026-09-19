from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.gateway.app import main
from apps.gateway.app.business_events import BusinessEventOutbox
from apps.gateway.app.campaign_dispatcher import (
    CampaignAudienceSnapshot,
    CampaignDispatchItem,
    CampaignDispatchRun,
    CampaignVersionSnapshot,
    cancel_campaign,
    dispatch_campaigns,
    pause_campaign,
    resume_campaign,
    schedule_campaign,
)
from apps.gateway.app.messaging import Template, TemplateVersion
from apps.gateway.app.preferences import ScopedSuppression
from apps.gateway.app.provider import SenderIdentity
from apps.gateway.app.production_api import (
    CampaignDispatchConfiguration,
    campaign_dispatch_configuration,
)


@pytest.fixture
def campaign_store(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (
        main.Tenant,
        main.Audit,
        main.Campaign,
        main.Contact,
        main.Suppression,
        ScopedSuppression,
        Template,
        TemplateVersion,
        SenderIdentity,
        CampaignVersionSnapshot,
        CampaignAudienceSnapshot,
        CampaignDispatchRun,
        CampaignDispatchItem,
        BusinessEventOutbox,
    ):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "DB", factory)
    monkeypatch.setenv("KLYROW_CAMPAIGN_DISPATCHER_ENABLED", "true")
    monkeypatch.setenv("KLYROW_CAMPAIGN_DISPATCHER_SANDBOX", "true")
    with factory() as session:
        session.add(main.Tenant(id="tenant-a", name="A", enabled=True, quota=10000))
        session.add(
            SenderIdentity(
                id="sender-a",
                tenant_id="tenant-a",
                domain_id="domain-a",
                email="marketing@example.com",
                stream="MARKETING",
                status="ACTIVE",
            )
        )
        session.add(
            Template(
                id="template-a",
                tenant_id="tenant-a",
                slug="launch",
                name="Launch",
                status="PUBLISHED",
                current_version=3,
                locale="en",
            )
        )
        session.add(
            TemplateVersion(
                id="template-version-a",
                tenant_id="tenant-a",
                template_id="template-a",
                version=3,
                subject="Frozen subject",
                html_body="<p>Hello</p>",
                text_body="Hello",
                variables_json="[]",
                created_by="owner-a",
            )
        )
        session.commit()
    yield factory
    engine.dispose()


def add_campaign(
    factory, *, campaign_id="campaign-a", recipients=("one@example.com",), when=None
):
    when = when or datetime.now(timezone.utc) - timedelta(seconds=1)
    with factory() as session:
        campaign = main.Campaign(
            id=campaign_id,
            tenant_id="tenant-a",
            name="Campaign",
            status="draft",
            sender_id="sender-a",
            template_id="template-a",
            current_version=4,
        )
        session.add(campaign)
        for index, recipient in enumerate(recipients):
            session.add(
                main.Contact(
                    id=f"contact-{campaign_id}-{index}",
                    tenant_id="tenant-a",
                    email=recipient,
                    subscribed=True,
                    metadata_json="{}",
                )
            )
        session.flush()
        run = schedule_campaign(session, campaign, when)
        session.commit()
        return run.id


def fake_success(monkeypatch, calls):
    import apps.gateway.app.provider as provider

    def send(payload, ctx, session, idempotency_key, correlation_id):
        calls.append((payload, ctx, idempotency_key, correlation_id))
        return {"message_id": "msg_" + idempotency_key[-16:], "status": "QUEUED"}

    monkeypatch.setattr(provider, "email_send", send)


def test_dispatch_configuration_is_idempotent_and_versions_changes(campaign_store):
    with campaign_store() as session:
        campaign = main.Campaign(
            id="campaign-config",
            tenant_id="tenant-a",
            name="Configuration",
            status="draft",
            current_version=1,
        )
        session.add(campaign)
        session.commit()
        context = {
            "tenant": "tenant-a",
            "sub": "marketer-a",
            "role": "MARKETING",
            "permissions": ["campaign.manage"],
        }
        body = CampaignDispatchConfiguration(
            sender_id="sender-a", template_id="template-a"
        )
        first = campaign_dispatch_configuration(campaign.id, body, context, session)
        second = campaign_dispatch_configuration(campaign.id, body, context, session)
        assert first == second
        assert first["campaign_version"] == 2
        assert campaign.sender_id == "sender-a"
        assert campaign.template_id == "template-a"


def test_campaign_freezes_version_and_deduplicates_recipient_identity(campaign_store):
    add_campaign(campaign_store, recipients=("One@Example.com", "one@example.com"))
    with campaign_store() as session:
        version = session.scalar(select(CampaignVersionSnapshot))
        assert version.campaign_version == 4
        assert version.template_version == 3
        assert version.subject == "Frozen subject"
        audience = list(session.scalars(select(CampaignAudienceSnapshot)))
        items = list(session.scalars(select(CampaignDispatchItem)))
        assert [row.recipient for row in audience] == ["one@example.com"]
        assert len(items) == 1


def test_campaign_normal_completion_uses_normal_message_admission(
    campaign_store, monkeypatch
):
    add_campaign(campaign_store)
    calls = []
    fake_success(monkeypatch, calls)
    assert dispatch_campaigns() == 1
    assert len(calls) == 1
    payload, ctx, key, correlation = calls[0]
    assert payload.stream == "MARKETING" and payload.sandbox is True
    assert ctx["service"] == "campaign-dispatcher"
    assert key.startswith("campaign:campaign-a:4:") and correlation.startswith(
        "cmpmsg_"
    )
    with campaign_store() as session:
        assert session.scalar(select(CampaignDispatchRun)).state == "COMPLETED"
        assert session.scalar(select(CampaignDispatchItem)).state == "DELIVERED"
        assert session.get(main.Campaign, "campaign-a").status == "completed"
        event = session.scalar(select(BusinessEventOutbox))
        assert event.event_type == "klyrow.campaign.summary"


def test_pause_resume_and_cancel_are_fenced_and_durable(campaign_store):
    add_campaign(campaign_store, when=datetime.now(timezone.utc) + timedelta(hours=1))
    with campaign_store() as session:
        campaign = session.get(main.Campaign, "campaign-a")
        run = pause_campaign(session, campaign)
        session.commit()
        assert run.state == "PAUSED" and campaign.status == "paused"
        run = resume_campaign(session, campaign)
        session.commit()
        assert run.state == "RETRYING" and campaign.status == "scheduled"
        run = cancel_campaign(session, campaign)
        session.commit()
        assert run.state == "CANCELLED" and campaign.status == "cancelled"
        assert session.scalar(select(CampaignDispatchItem)).state == "CANCELLED"
        assert (
            session.scalar(select(BusinessEventOutbox)).event_type
            == "klyrow.campaign.summary"
        )


def test_expired_dispatcher_lease_recovers_without_duplicate_send(
    campaign_store, monkeypatch
):
    add_campaign(campaign_store)
    with campaign_store() as session:
        run = session.scalar(select(CampaignDispatchRun))
        item = session.scalar(select(CampaignDispatchItem))
        run.state = "RUNNING"
        run.lease_owner = "crashed"
        run.fence_token = 7
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        item.state = "LEASED"
        item.lease_owner = "crashed"
        item.fence_token = 7
        session.commit()
    calls = []
    fake_success(monkeypatch, calls)
    assert dispatch_campaigns() == 1
    assert len(calls) == 1
    with campaign_store() as session:
        assert session.scalar(select(CampaignDispatchRun)).fence_token == 8
        assert session.scalar(select(CampaignDispatchItem)).attempt_count == 1


def test_active_dispatcher_lease_prevents_duplicate_worker(campaign_store, monkeypatch):
    add_campaign(campaign_store)
    with campaign_store() as session:
        run = session.scalar(select(CampaignDispatchRun))
        run.state = "RUNNING"
        run.lease_owner = "worker-a"
        run.fence_token = 1
        run.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        session.commit()
    calls = []
    fake_success(monkeypatch, calls)
    assert dispatch_campaigns() == 0
    assert calls == []


def test_suppressed_recipient_is_never_admitted(campaign_store, monkeypatch):
    add_campaign(campaign_store, recipients=("blocked@example.com", "ok@example.com"))
    with campaign_store() as session:
        session.add(
            main.Suppression(
                id="suppression-a",
                tenant_id="tenant-a",
                email="blocked@example.com",
                reason="complaint",
            )
        )
        session.commit()
    calls = []
    fake_success(monkeypatch, calls)
    assert dispatch_campaigns() == 2
    assert [str(call[0].recipient) for call in calls] == ["ok@example.com"]
    with campaign_store() as session:
        states = set(session.scalars(select(CampaignDispatchItem.state)))
        assert states == {"SUPPRESSED", "DELIVERED"}


def test_transient_admission_failure_retries_with_same_logical_key(
    campaign_store, monkeypatch
):
    add_campaign(campaign_store)
    import apps.gateway.app.provider as provider

    calls = []

    def flaky(payload, ctx, session, idempotency_key, correlation_id):
        calls.append(idempotency_key)
        if len(calls) == 1:
            raise HTTPException(503, "provider_unavailable")
        return {"message_id": "msg_recovered", "status": "QUEUED"}

    monkeypatch.setattr(provider, "email_send", flaky)
    assert dispatch_campaigns() == 1
    with campaign_store() as session:
        item = session.scalar(select(CampaignDispatchItem))
        run = session.scalar(select(CampaignDispatchRun))
        assert item.state == "RETRYING" and run.state == "RETRYING"
        item.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        run.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    assert dispatch_campaigns() == 1
    assert calls[0] == calls[1]
    with campaign_store() as session:
        assert session.scalar(select(CampaignDispatchItem)).state == "DELIVERED"
        assert (
            session.scalar(select(func.count()).select_from(BusinessEventOutbox)) == 1
        )
