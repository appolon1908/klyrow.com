from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.gateway.app.main import (
    Base,
    Campaign,
    Message,
    scoped_idempotency_key,
    semantic_request_hash,
)
from apps.gateway.app.production_api import CampaignSchedule, campaign_schedule, message_cancel


def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)()


def context():
    return {
        "sub": "caller-a",
        "tenant": "tenant-a",
        "role": "tenant_admin",
        "permissions": ["mail.send", "campaign.manage"],
    }


def test_message_cancel_replays_the_original_durable_result():
    s = session()
    s.add(
        Message(
            id="message-a",
            tenant_id="tenant-a",
            recipient="sink@example.invalid",
            sender="sender@example.invalid",
            subject="idempotency",
            status="queued",
        )
    )
    s.commit()
    first = message_cancel("message-a", context(), s, "cancel-message-key")
    second = message_cancel("message-a", context(), s, "cancel-message-key")
    assert first == second == {"id": "message-a", "status": "cancelled"}


def test_campaign_schedule_never_accepts_retries_or_changed_semantics_without_dispatcher():
    s = session()
    s.add(Campaign(id="campaign-a", tenant_id="tenant-a", name="Campaign", status="draft"))
    s.commit()
    first_time = datetime.now(UTC) + timedelta(hours=2)
    for scheduled_at in (first_time, first_time, first_time + timedelta(hours=1)):
        with pytest.raises(HTTPException) as refused:
            campaign_schedule(
                "campaign-a",
                CampaignSchedule(scheduled_at=scheduled_at),
                context(),
                s,
                "schedule-campaign-key",
            )
        assert refused.value.status_code == 409
        assert refused.value.detail == "campaign_dispatcher_unavailable"
    assert s.get(Campaign, "campaign-a").status == "draft"


def test_idempotency_identity_is_caller_bound():
    s = session()
    s.add(
        Message(
            id="message-a",
            tenant_id="tenant-a",
            recipient="sink@example.invalid",
            sender="sender@example.invalid",
            subject="idempotency",
            status="queued",
        )
    )
    s.commit()
    message_cancel("message-a", context(), s, "shared-raw-key")
    other = context()
    other["sub"] = "caller-b"
    with pytest.raises(HTTPException) as terminal:
        message_cancel("message-a", other, s, "shared-raw-key")
    assert terminal.value.status_code == 409
    assert terminal.value.detail == "terminal_message_cannot_cancel"


def test_storage_identity_binds_caller_service_action_version_and_resource():
    base = {"tenant": "tenant-a", "sub": "caller-a", "client_id": "portal"}
    original = scoped_idempotency_key(
        base, "shared-raw-key", action="message.send", resource="messages"
    )
    variants = (
        {**base, "tenant": "tenant-b"},
        {**base, "sub": "caller-b"},
        {**base, "client_id": "automation"},
    )
    assert all(
        scoped_idempotency_key(
            variant, "shared-raw-key", action="message.send", resource="messages"
        )
        != original
        for variant in variants
    )
    assert (
        scoped_idempotency_key(base, "shared-raw-key", action="message.cancel", resource="messages")
        != original
    )
    assert (
        scoped_idempotency_key(base, "shared-raw-key", action="message.send", resource="message-a")
        != original
    )
    assert (
        scoped_idempotency_key(
            base,
            "shared-raw-key",
            action="message.send",
            resource="messages",
            api_version="v2",
        )
        != original
    )


def test_semantic_hash_is_canonical_and_detects_payload_change():
    first = semantic_request_hash(
        action="message.send", resource="messages", payload={"b": 2, "a": 1}
    )
    reordered = semantic_request_hash(
        action="message.send", resource="messages", payload={"a": 1, "b": 2}
    )
    changed = semantic_request_hash(
        action="message.send", resource="messages", payload={"a": 1, "b": 3}
    )
    assert first == reordered
    assert first != changed
