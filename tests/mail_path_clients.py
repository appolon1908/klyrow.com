"""Reusable no-Postal clients for the three Klyrow mail submission paths."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app.main import (
    DB,
    AllowedSender,
    Base,
    Domain,
    Tenant,
    app,
    engine,
)
from apps.gateway.app import smtp_relay
from apps.gateway.app.provider import (
    ProviderDomain,
    ProviderMessage,
    SenderIdentity,
    SmtpCredential,
    TenantMailPolicy,
)

TENANT_ID = "mail-parity"
CONTEXT = {
    "sub": "mail-parity-test",
    "tenant": TENANT_ID,
    "role": "platform_admin",
    "service": True,
}
SINK = "capture@example.net"
EXTERNAL = "recipient@example.org"
ALL_PATHS = ["api", "smtp", "provider"]


@dataclass(frozen=True)
class Submission:
    accepted: bool
    status: str
    resource_id: str | None = None


def sender_for(stream: str) -> str:
    prefix = "marketing" if stream.upper() == "MARKETING" else "transactional"
    return prefix + "@m0.example"


def credential_id_for(stream: str) -> str:
    return "m0-" + stream.lower() + "-credential"


def seed_mail_paths() -> None:
    """Create equivalent API, SMTP, and provider identities and policies."""

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        session.add(Tenant(id=TENANT_ID, name="Mail parity", quota=10, enabled=True))
        session.add(
            Domain(
                id="m0-api-domain",
                tenant_id=TENANT_ID,
                domain="m0.example",
                token="verified",
                verified=True,
            )
        )
        session.add_all(
            [
                AllowedSender(
                    id="m0-api-transactional",
                    tenant_id=TENANT_ID,
                    address=sender_for("TRANSACTIONAL"),
                    role="mail",
                    enabled=True,
                ),
                AllowedSender(
                    id="m0-api-marketing",
                    tenant_id=TENANT_ID,
                    address=sender_for("MARKETING"),
                    role="mail",
                    enabled=True,
                ),
            ]
        )
        domain = ProviderDomain(
            id="m0-provider-domain",
            tenant_id=TENANT_ID,
            domain="m0.example",
            status="VERIFIED",
            ownership_token="verified",
        )
        session.add(domain)
        for stream in ("TRANSACTIONAL", "MARKETING"):
            sender = sender_for(stream)
            session.add(
                SenderIdentity(
                    id="m0-" + stream.lower() + "-sender",
                    tenant_id=TENANT_ID,
                    domain_id=domain.id,
                    email=sender,
                    stream=stream,
                    status="ACTIVE",
                )
            )
            session.add(
                SmtpCredential(
                    id=credential_id_for(stream),
                    tenant_id=TENANT_ID,
                    username="m0-" + stream.lower() + "-user",
                    secret_hash="unused-by-handler-fixture",
                    allowed_senders_json='["' + sender + '"]',
                    allowed_streams_json='["' + stream + '"]',
                )
            )
        session.add(
            TenantMailPolicy(
                tenant_id=TENANT_ID,
                sending_disabled=True,
                sandbox_mode=True,
                daily_limit=10,
                hourly_limit=10,
                warmup_daily_limit=10,
                warmup_hourly_limit=10,
                allowed_test_recipients_json='["capture@example.net"]',
                reputation_state="GOOD",
            )
        )
        session.commit()


class MailPathClients:
    """Submit equivalent messages without starting SMTP or Postal services."""

    def __init__(self) -> None:
        self.http = TestClient(app)

    def api(
        self,
        *,
        stream: str = "TRANSACTIONAL",
        recipient: str = SINK,
        key: str = "parity-api-key",
        subject: str = "Parity",
        **_ignored,
    ) -> Submission:
        response = self.http.post(
            "/v1/email/send",
            headers={"Idempotency-Key": key},
            json={
                "to": recipient,
                "sender": sender_for(stream),
                "subject": subject,
                "html": "<p>Parity</p>",
                "text": "Parity",
                "stream": stream.lower(),
            },
        )
        body = response.json()
        return Submission(
            response.status_code == 202,
            str(body.get("detail") or response.status_code),
            body.get("id"),
        )

    def provider(
        self,
        *,
        stream: str = "TRANSACTIONAL",
        recipient: str = SINK,
        key: str = "parity-provider-key",
        subject: str = "Parity",
        marketing_consent_granted: bool = False,
        **_ignored,
    ) -> Submission:
        response = self.http.post(
            "/v1/internal/email/send",
            headers={
                "Idempotency-Key": key,
                "X-Correlation-Id": "correlation-" + key,
            },
            json={
                "sender": sender_for(stream),
                "recipient": recipient,
                "subject": subject,
                "text": "Parity",
                "stream": stream.upper(),
                "sandbox": True,
                "marketing_consent_granted": marketing_consent_granted,
            },
        )
        body = response.json()
        return Submission(
            response.status_code == 202,
            str(body.get("detail") or response.status_code),
            body.get("message_id"),
        )

    def smtp(
        self,
        *,
        stream: str = "TRANSACTIONAL",
        recipient: str = SINK,
        key: str = "parity-smtp-key",
        subject: str = "Parity",
        **_ignored,
    ) -> Submission:
        relay = smtp_relay.GovernedRelay()
        smtp_session = SimpleNamespace(auth_data=credential_id_for(stream))
        message_id = "<" + key + "@m0.example>"
        raw = (
            f"From: {sender_for(stream)}\r\n"
            f"To: {recipient}\r\n"
            f"Message-ID: {message_id}\r\n"
            f"Subject: {subject}\r\n\r\nParity"
        ).encode()
        envelope = SimpleNamespace(mail_from=None, rcpt_tos=[], original_content=raw)
        mail_status = asyncio.run(
            relay.handle_MAIL(None, smtp_session, envelope, sender_for(stream), [])
        )
        if not mail_status.startswith("250"):
            return Submission(False, mail_status)
        recipient_status = asyncio.run(
            relay.handle_RCPT(None, smtp_session, envelope, recipient, [])
        )
        if not recipient_status.startswith("250"):
            return Submission(False, recipient_status)
        data_status = asyncio.run(relay.handle_DATA(None, smtp_session, envelope))
        with DB() as session:
            item = session.scalar(
                select(ProviderMessage)
                .where(ProviderMessage.tenant_id == TENANT_ID)
                .order_by(ProviderMessage.created_at.desc())
            )
        return Submission(
            data_status.startswith("250"),
            data_status,
            item.id if item else None,
        )

    def submit(self, path: str, **kwargs) -> Submission:
        return getattr(self, path)(**kwargs)
