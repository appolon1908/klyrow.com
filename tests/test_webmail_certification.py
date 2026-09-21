import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from apps.gateway.app.main import (
    AllowedSender,
    Base,
    DB,
    Domain,
    InboundRouteConfig,
    Tenant,
    engine,
)
from apps.gateway.app.provider import ProviderDomain
from apps.gateway.app import webmail
from apps.gateway.app.webmail import (
    AccessGrantIn,
    ComposeIn,
    DraftIn,
    MessageUpdate,
    activate_inbound_mailboxes,
    capture_provider_inbound,
    create_draft,
    delete_message,
    grant_access,
    list_access,
    list_mailboxes,
    revoke_access,
    send_message,
    update_draft,
    update_message,
)
from apps.gateway.app.webmail_models import WebmailAccess, WebmailMailbox, WebmailMessage


def reset_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def seed_tenants():
    with DB() as s:
        s.add_all([
            Tenant(id="tenant-a", name="Tenant A", quota=100),
            Tenant(id="tenant-b", name="Tenant B", quota=100),
        ])
        s.commit()


def mailbox(mailbox_id="mailbox-a", tenant="tenant-a", address="support@example.test"):
    return WebmailMailbox(
        id=mailbox_id,
        tenant_id=tenant,
        domain_id=f"domain-{tenant}",
        address=address,
        display_name="Support",
        status="ACTIVE",
        sending_enabled=True,
        receiving_enabled=True,
    )


def test_mailbox_grants_are_tenant_scoped_and_role_scoped():
    reset_db()
    seed_tenants()
    with DB() as s:
        box_a = mailbox()
        box_b = mailbox("mailbox-b", "tenant-b", "support@other.test")
        s.add_all([
            box_a,
            box_b,
            WebmailAccess(
                id="grant-reader",
                tenant_id="tenant-a",
                mailbox_id=box_a.id,
                user_id="reader-a",
                role="READER",
                created_by="owner-a",
            ),
            WebmailAccess(
                id="grant-sender",
                tenant_id="tenant-a",
                mailbox_id=box_a.id,
                user_id="sender-a",
                role="SENDER",
                created_by="owner-a",
            ),
        ])
        s.commit()

        reader = {"tenant": "tenant-a", "sub": "reader-a", "role": "SUPPORT"}
        sender = {"tenant": "tenant-a", "sub": "sender-a", "role": "DEVELOPER"}
        other_tenant_admin = {"tenant": "tenant-b", "sub": "admin-b", "role": "tenant_admin"}

        rows = list_mailboxes(reader, s)
        assert [item["id"] for item in rows] == ["mailbox-a"]

        with pytest.raises(HTTPException) as denied_send:
            create_draft(box_a.id, DraftIn(to="person@example.net", subject="x", text="y"), reader, None, s)
        assert denied_send.value.status_code == 404

        draft = create_draft(
            box_a.id,
            DraftIn(to="person@example.net", subject="Allowed", text="Draft body"),
            sender,
            None,
            s,
        )
        assert draft["folder"] == "DRAFTS"

        with pytest.raises(HTTPException) as denied_manage:
            update_message(box_a.id, draft["id"], MessageUpdate(folder="ARCHIVE"), sender, None, s)
        assert denied_manage.value.status_code == 404

        with pytest.raises(HTTPException) as cross_tenant:
            update_message(box_a.id, draft["id"], MessageUpdate(is_read=True), other_tenant_admin, None, s)
        assert cross_tenant.value.status_code == 404


def test_manager_grant_and_revoke_take_effect_immediately():
    reset_db()
    seed_tenants()
    owner = {"tenant": "tenant-a", "sub": "owner-a", "role": "OWNER"}
    user = {"tenant": "tenant-a", "sub": "dev-a", "role": "DEVELOPER"}
    with DB() as s:
        box = mailbox()
        s.add(box)
        s.commit()

        grant_access(box.id, AccessGrantIn(user_id="dev-a", role="OWNER"), owner, None, s)
        assert list_access(box.id, owner, s)[0]["role"] == "OWNER"
        assert list_mailboxes(user, s)[0]["id"] == box.id

        msg = WebmailMessage(
            id="message-a",
            tenant_id="tenant-a",
            mailbox_id=box.id,
            thread_id="thread-a",
            direction="INBOUND",
            folder="INBOX",
            from_address="sender@example.net",
            subject="Granted",
        )
        s.add(msg)
        s.commit()
        updated = update_message(box.id, msg.id, MessageUpdate(folder="ARCHIVE"), user, None, s)
        assert updated["folder"] == "ARCHIVE"

        revoke_access(box.id, "dev-a", owner, None, s)
        assert list_access(box.id, owner, s) == []
        assert list_mailboxes(user, s) == []
        with pytest.raises(HTTPException) as revoked:
            update_message(box.id, msg.id, MessageUpdate(is_read=True), user, None, s)
        assert revoked.value.status_code == 404


def test_draft_send_reply_and_retry_are_durable_and_idempotent(monkeypatch):
    reset_db()
    seed_tenants()
    owner = {"tenant": "tenant-a", "sub": "owner-a", "role": "OWNER"}
    with DB() as s:
        box = mailbox()
        parent = WebmailMessage(
            id="parent",
            tenant_id="tenant-a",
            mailbox_id=box.id,
            thread_id="thread-parent",
            direction="INBOUND",
            folder="INBOX",
            message_id_header="<parent@example.test>",
            from_address="sender@example.net",
            subject="Hello",
            text_body="Original",
        )
        s.add_all([box, parent])
        s.commit()

        draft = create_draft(
            box.id,
            DraftIn(
                to="sender@example.net",
                subject="Re: Hello",
                text="first draft",
                reply_to_message_id=parent.id,
            ),
            owner,
            None,
            s,
        )
        updated = update_draft(
            box.id,
            draft["id"],
            DraftIn(
                to="sender@example.net",
                subject="Re: Hello",
                text="<script>alert(1)</script>\nplain text",
                reply_to_message_id=parent.id,
            ),
            owner,
            None,
            s,
        )
        assert updated["thread_id"] == parent.thread_id

        fake_send = AsyncMock(return_value={"id": "core-message-1", "status": "accepted"})
        monkeypatch.setattr(webmail, "_send", fake_send)
        payload = ComposeIn(
            to="sender@example.net",
            subject="Re: Hello",
            text="<script>alert(1)</script>\nplain text",
            draft_id=draft["id"],
            reply_to_message_id=parent.id,
        )
        first = asyncio.run(send_message(box.id, payload, owner, None, s, "webmail-idempotency-1"))
        second = asyncio.run(send_message(box.id, payload, owner, None, s, "webmail-idempotency-1"))

        assert first["message"]["id"] == second["message"]["id"]
        assert first["message"]["thread_id"] == parent.thread_id
        assert first["message"]["in_reply_to"] == "<parent@example.test>"
        assert first["message"]["references"] == ["<parent@example.test>"]
        stored = s.scalar(select(WebmailMessage).where(WebmailMessage.outbound_message_id == "core-message-1"))
        assert stored.html_body == "<p>&lt;script&gt;alert(1)&lt;/script&gt;<br>plain text</p>"
        assert s.get(WebmailMessage, draft["id"]).deleted_at is not None
        assert len(s.scalars(select(WebmailMessage).where(WebmailMessage.direction == "OUTBOUND")).all()) == 1
        assert fake_send.await_count == 2


def test_message_state_requires_trash_before_permanent_delete():
    reset_db()
    seed_tenants()
    owner = {"tenant": "tenant-a", "sub": "owner-a", "role": "OWNER"}
    with DB() as s:
        box = mailbox()
        msg = WebmailMessage(
            id="message-a",
            tenant_id="tenant-a",
            mailbox_id=box.id,
            thread_id="thread-a",
            direction="INBOUND",
            folder="INBOX",
            from_address="sender@example.net",
            subject="Lifecycle",
        )
        s.add_all([box, msg])
        s.commit()

        update_message(box.id, msg.id, MessageUpdate(is_read=True, is_starred=True), owner, None, s)
        assert s.get(WebmailMessage, msg.id).is_read is True
        assert s.get(WebmailMessage, msg.id).is_starred is True

        with pytest.raises(HTTPException) as premature:
            delete_message(box.id, msg.id, True, owner, None, s)
        assert premature.value.status_code == 409

        delete_message(box.id, msg.id, False, owner, None, s)
        assert s.get(WebmailMessage, msg.id).folder == "TRASH"
        delete_message(box.id, msg.id, True, owner, None, s)
        assert s.get(WebmailMessage, msg.id).deleted_at is not None


def test_verified_domain_postal_reconciliation_activates_exact_route_and_projects_inbound(monkeypatch):
    reset_db()
    seed_tenants()
    owner = {"tenant": "tenant-a", "sub": "owner-a", "role": "OWNER"}

    postal_inbound = AsyncMock(return_value={"addresses": ["support@example.test"]})
    postal_outbound = AsyncMock(return_value={"domains": ["example.test"]})
    monkeypatch.setattr(webmail, "_reconcile_postal_inbound", postal_inbound)
    monkeypatch.setattr(webmail, "_reconcile_postal_outbound", postal_outbound)

    with DB() as s:
        s.add_all([
            Domain(
                id="domain-a",
                tenant_id="tenant-a",
                domain="example.test",
                token="verified-token",
                verified=True,
            ),
            ProviderDomain(
                id="provider-domain-a",
                tenant_id="tenant-a",
                domain="example.test",
                status="SENDING_ENABLED",
                ownership_token="owned",
                sending_enabled=True,
                inbound_enabled=False,
            ),
            AllowedSender(
                id="sender-support",
                tenant_id="tenant-a",
                address="support@example.test",
                role="support",
                enabled=True,
            ),
            Domain(
                id="domain-b",
                tenant_id="tenant-b",
                domain="other.test",
                token="verified-token-b",
                verified=True,
            ),
            ProviderDomain(
                id="provider-domain-b",
                tenant_id="tenant-b",
                domain="other.test",
                status="SENDING_ENABLED",
                ownership_token="owned-b",
                sending_enabled=True,
                inbound_enabled=False,
            ),
            AllowedSender(
                id="sender-b",
                tenant_id="tenant-b",
                address="support@other.test",
                role="support",
                enabled=True,
            ),
        ])
        s.commit()

        result = asyncio.run(activate_inbound_mailboxes(owner, None, s))
        assert result["activated_domains"] == 1
        assert result["activated_routes"] == 1
        assert result["domains"] == ["example.test"]
        postal_inbound.assert_awaited_once_with("tenant-a", ["support@example.test"])
        postal_outbound.assert_awaited_once_with(s, "tenant-a", ["example.test"])

        route = s.scalar(select(InboundRouteConfig).where(
            InboundRouteConfig.tenant_id == "tenant-a",
            InboundRouteConfig.address == "support@example.test",
        ))
        assert route is not None
        assert route.verified is True and route.enabled is True
        assert route.destination_kind == "webmail"
        assert route.destination_ref == "klyrow:webmail"

        box = s.scalar(select(WebmailMailbox).where(
            WebmailMailbox.tenant_id == "tenant-a",
            WebmailMailbox.address == "support@example.test",
        ))
        assert box is not None
        assert box.receiving_enabled is True
        assert s.scalar(select(InboundRouteConfig).where(InboundRouteConfig.tenant_id == "tenant-b")) is None

        attachment = b"controlled inbound evidence"
        parsed = {
            "message_id": "<postal-inbound-1@example.net>",
            "from": "External Sender <sender@example.net>",
            "to": "support@example.test",
            "cc": None,
            "subject": "Controlled inbound",
            "text": "Hello Klyrow inbox",
            "html": "<script>not rendered by bundled client</script>",
            "in_reply_to": None,
            "references": None,
            "attachments": [{
                "filename": "evidence.txt",
                "content_type": "text/plain",
                "size": len(attachment),
                "sha256": hashlib.sha256(attachment).hexdigest(),
            }],
            "attachment_contents": [attachment],
        }
        provider_item = SimpleNamespace(
            id="postal-inbound-1",
            tenant_id="tenant-a",
            recipient="support@example.test",
            disposition="ACCEPT",
        )
        message = capture_provider_inbound(s, route, provider_item, parsed)
        s.commit()

        assert message is not None
        assert message.folder == "INBOX"
        assert message.delivery_status == "RECEIVED"
        assert message.tenant_id == "tenant-a"
        assert message.mailbox_id == box.id
        assert "<script>not rendered" in (message.html_body or "")

        replay = capture_provider_inbound(s, route, provider_item, parsed)
        assert replay.id == message.id
        assert len(s.scalars(select(WebmailMessage).where(
            WebmailMessage.provider_inbound_id == "postal-inbound-1"
        )).all()) == 1
