import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from apps.gateway.app.main import DB, Base, InboundRouteConfig, Tenant, engine
from apps.gateway.app.webmail import capture_provider_inbound, delete_message, get_attachment
from apps.gateway.app.webmail_models import (
    WebmailAccess,
    WebmailAttachment,
    WebmailMailbox,
    WebmailMessage,
)


def _mailbox() -> WebmailMailbox:
    return WebmailMailbox(
        id="mailbox-1",
        tenant_id="tenant-a",
        domain_id="domain-1",
        address="support@example.test",
        display_name="Support",
        receiving_enabled=True,
    )


def _parsed() -> dict:
    content = b"durable attachment content"
    return {
        "message_id": "<provider-message@example.test>",
        "from": "Sender <sender@example.net>",
        "to": "support@example.test",
        "subject": "Attachment",
        "text": "See attachment",
        "html": None,
        "in_reply_to": None,
        "references": None,
        "attachments": [
            {
                "filename": "evidence.txt",
                "content_type": "text/plain",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "attachment_contents": [content],
    }


def test_non_webmail_route_cannot_copy_provider_mail_into_a_mailbox():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        route = InboundRouteConfig(
            id="route-1",
            tenant_id="tenant-a",
            address="support@example.test",
            destination_kind="webhook",
            destination_ref="support:external",
            verified=True,
            enabled=True,
        )
        session.add_all([Tenant(id="tenant-a", name="Tenant A", quota=100), _mailbox(), route])
        session.commit()
        provider_item = SimpleNamespace(
            id="provider-inbound-1",
            tenant_id="tenant-a",
            recipient="support@example.test",
            disposition="ACCEPT",
        )
        assert capture_provider_inbound(session, route, provider_item, _parsed()) is None
        assert session.scalar(select(WebmailMessage)) is None


def test_owned_route_persists_and_authorizes_attachment_download():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        route = InboundRouteConfig(
            id="route-1",
            tenant_id="tenant-a",
            address="support@example.test",
            destination_kind="webmail",
            destination_ref="klyrow:webmail",
            verified=True,
            enabled=True,
        )
        session.add_all([Tenant(id="tenant-a", name="Tenant A", quota=100), _mailbox(), route])
        session.commit()
        provider_item = SimpleNamespace(
            id="provider-inbound-1",
            tenant_id="tenant-a",
            recipient="support@example.test",
            disposition="ACCEPT",
        )
        message = capture_provider_inbound(session, route, provider_item, _parsed())
        session.commit()
        assert message is not None
        attachment = session.scalar(select(WebmailAttachment))
        assert attachment is not None
        assert attachment.content == b"durable attachment content"
        assert attachment.sha256 == hashlib.sha256(attachment.content).hexdigest()
        assert message is not None
        mailbox = session.get(WebmailMailbox, "mailbox-1")
        expected_bytes = len(b"AttachmentSee attachment") + len(attachment.content)
        assert mailbox.storage_used_bytes == expected_bytes
        response = get_attachment(
            "mailbox-1",
            message.id,
            attachment.id,
            {"sub": "owner", "tenant": "tenant-a", "role": "platform_admin"},
            session,
        )
        assert response.body == attachment.content
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "evidence.txt" in response.headers["content-disposition"]


def test_duplicate_attachments_are_preserved_and_quota_fails_closed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        route = InboundRouteConfig(
            id="route-1",
            tenant_id="tenant-a",
            address="support@example.test",
            destination_kind="webmail",
            destination_ref="klyrow:webmail",
            verified=True,
            enabled=True,
        )
        mailbox = _mailbox()
        mailbox.storage_quota_bytes = 10_000
        session.add_all([Tenant(id="tenant-a", name="Tenant A", quota=100), mailbox, route])
        session.commit()
        parsed = _parsed()
        parsed["attachments"] = parsed["attachments"] * 2
        parsed["attachment_contents"] = parsed["attachment_contents"] * 2
        provider_item = SimpleNamespace(
            id="provider-inbound-duplicates",
            tenant_id="tenant-a",
            recipient="support@example.test",
            disposition="ACCEPT",
        )
        capture_provider_inbound(session, route, provider_item, parsed)
        session.commit()
        assert len(session.scalars(select(WebmailAttachment)).all()) == 2

        mailbox.storage_quota_bytes = mailbox.storage_used_bytes
        provider_item.id = "provider-inbound-over-quota"
        parsed["message_id"] = "<second@example.test>"
        with pytest.raises(HTTPException) as denied:
            capture_provider_inbound(session, route, provider_item, parsed)
        assert denied.value.status_code == 507
        assert denied.value.detail == "mailbox_storage_quota_exceeded"


def test_reader_cannot_move_or_delete_shared_mailbox_messages():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        mailbox = _mailbox()
        message = WebmailMessage(
            id="message-1",
            tenant_id="tenant-a",
            mailbox_id=mailbox.id,
            thread_id="thread-1",
            direction="INBOUND",
            folder="INBOX",
            from_address="sender@example.net",
            subject="Shared",
        )
        access = WebmailAccess(
            id="access-1",
            tenant_id="tenant-a",
            mailbox_id=mailbox.id,
            user_id="reader-1",
            role="READER",
            created_by="owner-1",
        )
        session.add_all(
            [Tenant(id="tenant-a", name="Tenant A", quota=100), mailbox, message, access]
        )
        session.commit()
        ctx = {"sub": "reader-1", "tenant": "tenant-a", "role": "SUPPORT"}
        with pytest.raises(HTTPException) as denied:
            delete_message(mailbox.id, message.id, False, ctx, None, session)
        assert denied.value.status_code == 404
        assert session.get(WebmailMessage, message.id).folder == "INBOX"
