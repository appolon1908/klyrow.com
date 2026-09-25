import asyncio
import json
from email.message import EmailMessage

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from apps.gateway.app import guards, main, operations, webmail
from apps.gateway.app.main import (
    AllowedSender,
    Base,
    DB,
    Domain,
    EmailOutbox,
    Message,
    Suppression,
    Tenant,
    engine,
)
from apps.gateway.app.provider import parse_inbound
from apps.gateway.app.saas import Consent, Preference, Profile
from apps.gateway.app.webmail import ComposeIn, send_message
from apps.gateway.app.webmail_models import WebmailMailbox, WebmailMessage


def reset_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def seed_webmail_sender(session):
    session.add_all([
        Tenant(id="tenant-a", name="Tenant A", quota=100),
        Domain(
            id="domain-a",
            tenant_id="tenant-a",
            domain="example.com",
            token="verified",
            verified=True,
        ),
        AllowedSender(
            id="sender-a",
            tenant_id="tenant-a",
            address="support@example.com",
            role="support",
            enabled=True,
        ),
        WebmailMailbox(
            id="mailbox-a",
            tenant_id="tenant-a",
            domain_id="domain-a",
            address="support@example.com",
            display_name="Support",
            status="ACTIVE",
            sending_enabled=True,
            receiving_enabled=True,
        ),
    ])
    session.commit()


def test_webmail_outbound_creates_real_outbox_and_postal_fact_updates_sent_projection(monkeypatch):
    reset_db()
    owner = {
        "tenant": "tenant-a",
        "sub": "owner-a",
        "role": "OWNER",
        "browser": True,
    }

    monkeypatch.setattr(main, "SAFE_MODE", False)
    monkeypatch.setattr(main, "enforce_production_canary", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "enforce_tenant_send_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(guards, "billing_identity", lambda *args, **kwargs: ("sub-cert", "price-cert"))

    with DB() as session:
        seed_webmail_sender(session)
        result = asyncio.run(send_message(
            "mailbox-a",
            ComposeIn(
                to="recipient@example.net",
                subject="Controlled Webmail send",
                text="No external worker execution in this test.",
            ),
            owner,
            None,
            session,
            "webmail-outbound-cert-1",
        ))

        core_id = result["delivery"]["id"]
        stored = session.scalar(select(WebmailMessage).where(
            WebmailMessage.tenant_id == "tenant-a",
            WebmailMessage.outbound_message_id == core_id,
        ))
        assert stored is not None
        assert stored.folder == "SENT"
        assert stored.delivery_status == "QUEUED"

        outbox = session.scalar(select(EmailOutbox).where(
            EmailOutbox.tenant_id == "tenant-a",
            EmailOutbox.message_id == core_id,
        ))
        assert outbox is not None
        payload = json.loads(outbox.payload)
        assert payload["from"] == "support@example.com"
        assert payload["to"] == ["recipient@example.net"]
        assert payload["stream"] == "transactional"
        assert payload["subject"] == "Controlled Webmail send"
        assert session.get(Message, core_id).status == "queued"

        # Simulate the durable provider readback. No provider request is issued.
        outbox.provider_message_id = "postal-provider-cert-1"
        session.commit()
        status = main.persist_email_event(
            session,
            event_id="postal-event-cert-1",
            tenant_id="tenant-a",
            message_id="postal-provider-cert-1",
            correlation_id=core_id,
            event_type="email.delivered",
            recipient="recipient@example.net",
            raw_status="2.0.0 delivered",
            payload="{}",
        )
        session.refresh(stored)
        assert status == "delivered"
        assert stored.delivery_status == "DELIVERED"
        assert session.get(Message, core_id).status == "delivered"


def _raw_attachment(filename: str, payload: bytes) -> bytes:
    message = EmailMessage()
    message["From"] = "sender@example.net"
    message["To"] = "support@example.com"
    message["Subject"] = "Attachment certification"
    message.set_content("Plain body")
    message.add_attachment(
        payload,
        maintype="application",
        subtype="octet-stream",
        filename=filename,
    )
    return message.as_bytes()


def test_inbound_attachment_limits_and_executable_quarantine():
    safe = parse_inbound(
        _raw_attachment("report.pdf", b"safe-payload"),
        max_message_bytes=100_000,
        max_attachment_bytes=10_000,
    )
    assert safe["disposition"] == "ACCEPT"
    assert safe["attachments"][0]["filename"] == "report.pdf"
    assert safe["attachments"][0]["size"] == len(b"safe-payload")

    executable = parse_inbound(
        _raw_attachment("payload.exe", b"not-executed"),
        max_message_bytes=100_000,
        max_attachment_bytes=10_000,
    )
    assert executable["disposition"] == "QUARANTINE"

    with pytest.raises(HTTPException) as oversized:
        parse_inbound(
            _raw_attachment("large.bin", b"x" * 32),
            max_message_bytes=100_000,
            max_attachment_bytes=8,
        )
    assert oversized.value.status_code == 413
    assert oversized.value.detail == "inbound_attachment_too_large"


def test_inbound_attachment_path_traversal_is_rejected():
    boundary = "klyrow-cert-boundary"
    raw = (
        "From: sender@example.net\r\n"
        "To: support@example.com\r\n"
        "Subject: Traversal\r\n"
        "MIME-Version: 1.0\r\n"
        f"Content-Type: multipart/mixed; boundary=\"{boundary}\"\r\n"
        "\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "hello\r\n"
        f"--{boundary}\r\n"
        "Content-Type: application/octet-stream\r\n"
        "Content-Disposition: attachment; filename=\"../secret.txt\"\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        "c2VjcmV0\r\n"
        f"--{boundary}--\r\n"
    ).encode("ascii")
    with pytest.raises(HTTPException) as denied:
        parse_inbound(raw, max_message_bytes=100_000, max_attachment_bytes=10_000)
    assert denied.value.status_code == 422
    assert denied.value.detail == "unsafe_attachment_filename"


def test_transactional_webmail_does_not_require_marketing_consent_but_hard_suppression_still_blocks():
    reset_db()
    with DB() as session:
        session.add(Tenant(id="tenant-a", name="Tenant A", quota=100))
        session.commit()

        allowed = guards.authorize_send(
            session,
            tenant_id="tenant-a",
            sender="support@example.com",
            recipient="person@example.net",
            stream="transactional",
            sandbox=False,
        )
        assert allowed["stream"] == "transactional"

        session.add(Suppression(
            id="suppression-hard",
            tenant_id="tenant-a",
            email="person@example.net",
            reason="complaint",
        ))
        session.commit()
        with pytest.raises(HTTPException) as hard_block:
            guards.authorize_send(
                session,
                tenant_id="tenant-a",
                sender="support@example.com",
                recipient="person@example.net",
                stream="transactional",
                sandbox=False,
            )
        assert hard_block.value.detail == "recipient_suppressed"


def test_marketing_requires_stored_consent_and_respects_marketing_suppression():
    reset_db()
    with DB() as session:
        session.add(Tenant(id="tenant-a", name="Tenant A", quota=100))
        session.commit()

        with pytest.raises(HTTPException) as missing_consent:
            guards.authorize_send(
                session,
                tenant_id="tenant-a",
                sender="marketing@example.com",
                recipient="person@example.net",
                stream="marketing",
                sandbox=False,
                topic="marketing",
            )
        assert missing_consent.value.detail == "marketing_consent_required"

        profile = Profile(
            id="profile-a",
            tenant_id="tenant-a",
            email="person@example.net",
        )
        session.add_all([
            profile,
            Preference(
                id="pref-a",
                tenant_id="tenant-a",
                profile_id=profile.id,
                topic="marketing",
                subscribed=True,
            ),
            Consent(
                id="consent-a",
                tenant_id="tenant-a",
                profile_id=profile.id,
                topic="marketing",
                status="granted",
                source="certification",
                version="v1",
            ),
        ])
        session.commit()

        allowed = guards.authorize_send(
            session,
            tenant_id="tenant-a",
            sender="marketing@example.com",
            recipient="person@example.net",
            stream="marketing",
            sandbox=False,
            topic="marketing",
        )
        assert allowed["stream"] == "marketing"

        session.add(Suppression(
            id="suppression-marketing",
            tenant_id="tenant-a",
            email="person@example.net",
            reason="unsubscribe_marketing",
        ))
        session.commit()
        with pytest.raises(HTTPException) as suppressed:
            guards.authorize_send(
                session,
                tenant_id="tenant-a",
                sender="marketing@example.com",
                recipient="person@example.net",
                stream="marketing",
                sandbox=False,
                topic="marketing",
            )
        assert suppressed.value.detail == "recipient_suppressed"
