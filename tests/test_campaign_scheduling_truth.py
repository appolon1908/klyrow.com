import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from apps.gateway.app import main as core
from apps.gateway.app.messaging import CampaignDefinition


@pytest.mark.parametrize("kind", ["campaigns", "campaign-definitions"])
def test_no_dispatcher_means_no_accepted_schedule_or_replay(gateway, kind):
    client, sessions, context = gateway
    context.update(role="MARKETING", permissions=["campaign.manage"])
    with sessions() as session:
        if kind == "campaigns":
            item = core.Campaign(id="campaign-a", tenant_id="tenant-a", name="Test", status="draft")
        else:
            item = CampaignDefinition(
                id="campaign-a",
                tenant_id="tenant-a",
                name="Test",
                sender_id="sender-a",
                template_id="template-a",
                status="TESTING",
                test_sent_at=datetime.now(UTC),
            )
        session.add(item)
        session.commit()
    payload = {"scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    for _ in range(2):
        response = client.post(
            f"/v1/{kind}/campaign-a/schedule",
            json=payload,
            headers={"Idempotency-Key": "schedule-test-key"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "campaign_dispatcher_unavailable"
    with sessions() as session:
        assert session.get(type(item), "campaign-a").scheduled_at is None
        assert session.scalar(select(func.count()).select_from(core.Idempotency)) == 0
        assert session.scalar(select(func.count()).select_from(core.EmailOutbox)) == 0


@pytest.mark.parametrize("kind", ["campaigns", "campaign-definitions"])
def test_schedule_denies_read_only_before_state_access(gateway, kind):
    client, _, context = gateway
    context.update(role="READ_ONLY", permissions=["mail.read"])
    response = client.post(
        f"/v1/{kind}/foreign/schedule",
        json={"scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        headers={"Idempotency-Key": "schedule-test-key"},
    )
    assert response.status_code == 403


def test_old_accepted_schedule_is_not_replayed(gateway):
    client, sessions, context = gateway
    context.update(role="MARKETING", permissions=["campaign.manage"])
    payload = {"scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    with sessions() as session:
        session.add(
            core.Campaign(id="campaign-a", tenant_id="tenant-a", name="Legacy", status="scheduled")
        )
        session.add(
            core.Idempotency(
                tenant_id="tenant-a",
                resource_id="campaign-a",
                key=core.scoped_idempotency_key(
                    context, "schedule-test-key", action="campaign.schedule", resource="campaign-a"
                ),
                request_hash=core.semantic_request_hash(
                    action="campaign.schedule", resource="campaign-a", payload=payload
                ),
                response_json=json.dumps({"id": "campaign-a", "status": "scheduled"}),
            )
        )
        session.commit()
    response = client.post(
        "/v1/campaigns/campaign-a/schedule",
        json=payload,
        headers={"Idempotency-Key": "schedule-test-key"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "campaign_dispatcher_unavailable"


@pytest.mark.parametrize("kind", ["campaigns", "campaign-definitions"])
def test_schedule_retains_tenant_isolation(gateway, kind):
    client, sessions, context = gateway
    context.update(role="MARKETING", permissions=["campaign.manage"])
    with sessions() as session:
        if kind == "campaigns":
            item = core.Campaign(id="foreign", tenant_id="tenant-b", name="Foreign", status="draft")
        else:
            item = CampaignDefinition(
                id="foreign",
                tenant_id="tenant-b",
                name="Foreign",
                sender_id="sender-b",
                template_id="template-b",
                status="TESTING",
                test_sent_at=datetime.now(UTC),
            )
        session.add(item)
        session.commit()
    response = client.post(
        f"/v1/{kind}/foreign/schedule",
        json={"scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        headers={"Idempotency-Key": "schedule-test-key"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("kind", ["campaigns", "campaign-definitions"])
def test_resolver_authorizes_the_campaign_capability_before_schedule_refusal(
    gateway, monkeypatch, kind
):
    import httpx

    client, sessions, _ = gateway
    core.app.dependency_overrides.pop(core.auth)
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "https://resolver.invalid/resolve")
    with sessions() as session:
        if kind == "campaigns":
            item = core.Campaign(id="campaign-a", tenant_id="tenant-a", name="Test", status="draft")
        else:
            item = CampaignDefinition(
                id="campaign-a",
                tenant_id="tenant-a",
                name="Test",
                sender_id="sender-a",
                template_id="template-a",
                status="TESTING",
                test_sent_at=datetime.now(UTC),
            )
        session.add(item)
        session.commit()
    permissions = []

    def resolve(url, *, headers, **kwargs):
        permission = headers["X-Codestra-Required-Permission"]
        permissions.append(permission)
        assert headers["X-Klyrow-Tenant-Id"] == "tenant-a"
        return httpx.Response(
            200,
            json={
                "authorized": True,
                "permission": permission,
                "identity_id": "campaign-service",
                "tenant_id": "tenant-a",
                "role": "service",
            },
        )

    monkeypatch.setattr(core.httpx, "get", resolve)
    headers = {
        "Authorization": "Bearer synthetic-campaign-token",
        "X-Tenant-ID": "tenant-a",
        "Idempotency-Key": "schedule-test-key",
    }
    payload = {"scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    response = client.post(f"/v1/{kind}/campaign-a/schedule", headers=headers, json=payload)
    assert permissions == ["campaign.manage"]
    assert response.status_code == 409
    assert response.json()["detail"] == "campaign_dispatcher_unavailable"
    monkeypatch.setattr(core.httpx, "get", lambda *a, **k: httpx.Response(403))
    assert (
        client.post(f"/v1/{kind}/campaign-a/schedule", headers=headers, json=payload).status_code
        == 403
    )
