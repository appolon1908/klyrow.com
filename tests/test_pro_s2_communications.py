import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from apps.gateway.app.browser_email_setup import (
    browser_deliverability,
    browser_domain_detail,
    browser_sender_detail,
)
from apps.gateway.app.browser_profiles_suppressions import (
    BrowserSuppressionIn,
    add_suppression,
    remove_suppression,
    suppressions,
)
from apps.gateway.app.main import (
    Audit,
    Base,
    DB,
    Domain,
    EmailOutbox,
    Event,
    Message,
    PostalEvent,
    Suppression,
    Tenant,
    engine,
)
from apps.gateway.app.messaging import DkimKeyVersion, DomainClaim, SenderIdentity
from apps.gateway.app.provider import ProviderDomain
from apps.gateway.app.saas import DeliverabilitySnapshot
from apps.gateway.app.tenancy_onboarding import (
    browser_message_detail,
    browser_message_events,
)
from apps.gateway.app.webmail_models import WebmailAccess, WebmailMailbox


NOW = datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc)


def reset_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def ctx(tenant="tenant-a", sub="owner-a", role="OWNER"):
    return {"tenant": tenant, "sub": sub, "role": role}


def seed_tenants(s):
    s.add_all([
        Tenant(id="tenant-a", name="Tenant A", quota=1000),
        Tenant(id="tenant-b", name="Tenant B", quota=1000),
    ])


def seed_domain_stack(s):
    claim_a = DomainClaim(
        id="claim-a",
        tenant_id="tenant-a",
        domain="example.test",
        state="VERIFIED",
        challenge_hash="challenge-a",
        dkim_selector="kly-a",
        dkim_version=2,
        return_path="bounce.example.test",
        tracking_domain="track.example.test",
        verified_at=NOW,
        created_at=NOW,
    )
    claim_b = DomainClaim(
        id="claim-b",
        tenant_id="tenant-b",
        domain="other.test",
        state="VERIFIED",
        challenge_hash="challenge-b",
        dkim_selector="kly-b",
        dkim_version=1,
        return_path="bounce.other.test",
        tracking_domain="track.other.test",
        verified_at=NOW,
        created_at=NOW,
    )
    domain_a = Domain(
        id="domain-a",
        tenant_id="tenant-a",
        domain="example.test",
        token="dns-verified",
        verified=True,
    )
    s.add_all([
        claim_a,
        claim_b,
        domain_a,
        DkimKeyVersion(
            id="dkim-a",
            tenant_id="tenant-a",
            domain_claim_id=claim_a.id,
            selector="kly-a",
            version=2,
            public_key="public-only",
            private_key_reference="openbao://secret/private",
            active=True,
            created_at=NOW,
        ),
        ProviderDomain(
            id="provider-domain-a",
            tenant_id="tenant-a",
            domain="example.test",
            status="SENDING_ENABLED",
            ownership_token="provider-secret",
            verified_at=NOW,
            dkim_selector="postal",
            dkim_key_version=1,
            sending_enabled=True,
            inbound_enabled=True,
            created_at=NOW,
        ),
        DeliverabilitySnapshot(
            id="snapshot-a",
            tenant_id="tenant-a",
            domain_id=domain_a.id,
            spf=True,
            dkim=True,
            dmarc=True,
            mx=True,
            ptr=False,
            tls=False,
            details_json=json.dumps({
                "alerts": [
                    {"severity": "critical", "code": "ptr_missing"},
                    {"severity": "critical", "code": "tls_missing"},
                ],
                "raw_provider_secret": "must-not-leak",
            }),
            checked_at=NOW,
        ),
    ])
    return claim_a, claim_b


def test_domain_detail_is_tenant_scoped_and_redacts_provider_secrets():
    reset_db()
    with DB() as s:
        seed_tenants(s)
        seed_domain_stack(s)
        s.commit()

        result = browser_domain_detail("claim-a", ctx(), s)
        serialized = json.dumps(result, default=str)

        assert result["domain"] == "example.test"
        assert result["dkim"]["history"][0]["selector"] == "kly-a"
        assert result["provider_readiness"] == {
            "sending_enabled": True,
            "inbound_enabled": True,
            "status": "SENDING_ENABLED",
        }
        assert result["deliverability"]["alerts"][0]["code"] == "ptr_missing"
        assert "private_key_reference" not in serialized
        assert "openbao://secret/private" not in serialized
        assert "ownership_token" not in serialized
        assert "provider-secret" not in serialized
        assert "must-not-leak" not in serialized

        with pytest.raises(HTTPException) as denied:
            browser_domain_detail("claim-b", ctx(), s)
        assert denied.value.status_code == 404


def test_sender_detail_reuses_domain_and_shared_mailbox_authority():
    reset_db()
    with DB() as s:
        seed_tenants(s)
        claim_a, _ = seed_domain_stack(s)
        sender = SenderIdentity(
            id="sender-a",
            tenant_id="tenant-a",
            domain_claim_id=claim_a.id,
            address="support@example.test",
            display_name="Support",
            reply_to=None,
            stream="TRANSACTIONAL",
            status="ACTIVE",
            verified=True,
        )
        mailbox = WebmailMailbox(
            id="mailbox-a",
            tenant_id="tenant-a",
            domain_id="provider-domain-a",
            address=sender.address,
            display_name="Support",
            status="ACTIVE",
            sending_enabled=True,
            receiving_enabled=True,
        )
        s.add_all([
            sender,
            mailbox,
            WebmailAccess(
                id="grant-a",
                tenant_id="tenant-a",
                mailbox_id=mailbox.id,
                user_id="reader-a",
                role="READER",
                created_by="owner-a",
            ),
        ])
        s.commit()

        result = browser_sender_detail(sender.id, ctx(), s)
        assert result["domain"] == {
            "id": claim_a.id,
            "domain": claim_a.domain,
            "state": "VERIFIED",
        }
        assert result["mailbox"] == {
            "id": mailbox.id,
            "sending_enabled": True,
            "receiving_enabled": True,
            "shared_grant_count": 1,
        }


def test_deliverability_list_is_bounded_tenant_scoped_and_reports_stale_unknown_honestly():
    reset_db()
    with DB() as s:
        seed_tenants(s)
        seed_domain_stack(s)
        s.commit()

        result = browser_deliverability(
            limit=100,
            offset=0,
            state=None,
            ctx=ctx(),
            s=s,
        )
        assert [item["id"] for item in result["items"]] == ["claim-a"]
        row = result["items"][0]
        assert row["source"] == "durable_snapshot"
        assert row["spf"] is True
        assert row["ptr"] is False
        assert row["alert_count"] == 2
        assert row["provider_status"] == "SENDING_ENABLED"


def test_message_detail_normalizes_delivery_evidence_and_never_returns_raw_payloads():
    reset_db()
    with DB() as s:
        seed_tenants(s)
        message = Message(
            id="message-a",
            tenant_id="tenant-a",
            recipient="person@example.net",
            sender="support@example.test",
            subject="Evidence",
            status="accepted",
            created_at=NOW,
        )
        s.add_all([
            message,
            Message(
                id="message-b",
                tenant_id="tenant-b",
                recipient="other@example.net",
                sender="support@other.test",
                subject="Other tenant",
                status="accepted",
                created_at=NOW,
            ),
            EmailOutbox(
                id="outbox-a",
                tenant_id="tenant-a",
                message_id=message.id,
                operation_id="operation-a",
                correlation_id="correlation-a",
                payload=json.dumps({"secret_body": "do-not-return"}),
                state="sent",
                attempts=1,
                provider_message_id="provider-reference",
                created_at=NOW,
                updated_at=NOW,
            ),
            Event(
                id="event-a",
                tenant_id="tenant-a",
                message_id=message.id,
                kind="email.delivered",
                payload=json.dumps({"provider_response": "raw-secret"}),
                created_at=NOW,
            ),
            PostalEvent(
                id="postal-a",
                event_type="email.delivered",
                correlation_id="correlation-a",
                message_id=message.id,
                tenant_id="tenant-a",
                payload=json.dumps({"raw_postal": "postal-secret"}),
                state="processed",
                attempts=1,
                created_at=NOW,
                updated_at=NOW,
            ),
        ])
        s.commit()

        result = browser_message_detail(message.id, ctx(), s)
        serialized = json.dumps(result, default=str)
        assert result["correlation_id"] == "correlation-a"
        assert result["operation_id"] == "operation-a"
        assert result["current_outcome"] == "DELIVERED"
        assert any(entry["status"] == "DELIVERED" for entry in result["timeline"])
        assert "secret_body" not in serialized
        assert "raw-secret" not in serialized
        assert "postal-secret" not in serialized
        assert '"payload"' not in serialized

        first = browser_message_events(
            message.id,
            limit=1,
            cursor=None,
            ctx=ctx(),
            s=s,
        )
        assert len(first["items"]) == 1
        assert first["next_cursor"] == "1"
        second = browser_message_events(
            message.id,
            limit=50,
            cursor=first["next_cursor"],
            ctx=ctx(),
            s=s,
        )
        assert second["items"]
        assert second["next_cursor"] is None

        with pytest.raises(HTTPException) as denied:
            browser_message_detail("message-b", ctx(), s)
        assert denied.value.status_code == 404


def test_suppression_filters_and_mutations_are_tenant_scoped_and_audited():
    reset_db()
    with DB() as s:
        seed_tenants(s)
        s.add_all([
            Suppression(id="supp-a", tenant_id="tenant-a", email="first@example.net", reason="bounce"),
            Suppression(id="supp-b", tenant_id="tenant-a", email="second@example.net", reason="complaint"),
            Suppression(id="supp-other", tenant_id="tenant-b", email="other@example.net", reason="bounce"),
        ])
        s.commit()

        filtered = suppressions(
            limit=100,
            offset=0,
            q="first@",
            reason="bounce",
            ctx=ctx(),
            s=s,
        )
        assert [item["id"] for item in filtered["items"]] == ["supp-a"]

        created = add_suppression(
            BrowserSuppressionIn(email=" New@Example.Net ", reason="manual"),
            ctx(),
            None,
            s,
            "suppression-key-123",
        )
        assert created["email"] == "new@example.net"
        assert s.scalar(select(Audit).where(
            Audit.tenant_id == "tenant-a",
            Audit.action == "suppression.added",
        )) is not None

        remove_suppression(created["id"], ctx(), None, s)
        assert s.get(Suppression, created["id"]) is None
        assert s.scalar(select(Audit).where(
            Audit.tenant_id == "tenant-a",
            Audit.action == "suppression.removed",
        )) is not None

        with pytest.raises(HTTPException) as denied:
            remove_suppression("supp-other", ctx(), None, s)
        assert denied.value.status_code == 404
