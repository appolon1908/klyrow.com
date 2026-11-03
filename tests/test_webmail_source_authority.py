import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from apps.gateway.app.main import DB, Base, MailIn, Tenant, engine
from apps.gateway.app import agent_mailboxes, postal_provisioning
from apps.gateway.app.postal_provisioning import PostalDomainCredential
from apps.gateway.app.webmail import router as webmail_router

ROOT = Path(__file__).resolve().parents[1]


def test_webmail_routes_are_registered_in_platform_runtime():
    from apps.gateway.app.platform import app

    live = {
        (method, route.path) for route in app.routes for method in getattr(route, "methods", set())
    }
    expected = {
        (method, route.path)
        for route in webmail_router.routes
        for method in getattr(route, "methods", set())
    }
    assert expected
    assert expected <= live


def test_web_candidate_contains_the_reviewed_webmail_application():
    entrypoint = (ROOT / "apps/web/src/main.ts").read_text(encoding="utf-8")
    dashboard = (ROOT / "apps/web/src/Dashboard.vue").read_text(encoding="utf-8")
    webmail = (ROOT / "apps/web/src/Webmail.vue").read_text(encoding="utf-8")
    assert "import Webmail from './Webmail.vue'" in entrypoint
    route_manifest = (ROOT / "apps/web/src/routeManifest.ts").read_text(encoding="utf-8")
    assert "browserRoute(path)" in entrypoint
    assert "path: '/app/mail', descendants: true, view: 'Webmail'" in route_manifest
    assert 'href="/app/mail"' in dashboard
    assert "'/app/api/mailboxes'" in webmail
    assert "Idempotency-Key" in webmail
    assert "composeSendKey.value" in webmail
    assert ':href="file.download_url"' in webmail
    assert "v-html" not in webmail


def test_mailbox_materialization_is_serialized_on_postgresql():
    source = (ROOT / "apps/gateway/app/webmail.py").read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in source
    assert 's.get_bind().dialect.name == "postgresql"' in source


def test_webmail_is_the_only_campaignless_browser_mail_channel(monkeypatch):
    monkeypatch.setenv("KLYROW_CAMPAIGN_REQUIRED", "true")
    with DB() as session:
        agent_mailboxes.authorize_agent_sender(
            session,
            {"browser": True, "_klyrow_mail_channel": "webmail"},
            "sender@example.test",
            None,
        )
        with pytest.raises(Exception) as denied:
            agent_mailboxes.authorize_agent_sender(
                session, {"browser": True}, "sender@example.test", None
            )
    assert getattr(denied.value, "status_code", None) == 403


def test_webmail_threading_headers_are_governed_and_persisted_for_delivery():
    headers = {
        "Message-ID": "<message-1@webmail.example.test>",
        "In-Reply-To": "<parent-1@webmail.example.test>",
        "References": "<root-1@webmail.example.test> <parent-1@webmail.example.test>",
    }
    payload = MailIn(
        to="recipient@example.com",
        sender="sender@example.com",
        subject="Reply",
        html="<p>Reply</p>",
        headers=headers,
    )
    assert payload.headers == headers
    with pytest.raises(ValueError, match="invalid_mail_header"):
        MailIn(
            to="recipient@example.com",
            sender="sender@example.com",
            subject="Unsafe",
            html="<p>Unsafe</p>",
            headers={"Message-ID": "<safe@example.test>\r\nBcc: victim@example.test"},
        )
    main_source = (ROOT / "apps/gateway/app/main.py").read_text(encoding="utf-8")
    assert "delivery_headers = dict(x.headers)" in main_source
    assert 'delivery_payload["headers"] = delivery_headers' in main_source


def test_live_domain_reconciliation_persists_only_attested_owner(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        postal_provisioning,
        "_call_live_domain_bridge",
        AsyncMock(
            return_value={
                "domains": [
                    {
                        "domain": "mail.example.test",
                        "server_id": "server-1",
                        "server_permalink": "server-1",
                        "mode": "Live",
                        "api_key": "test-provider-key-material-123456",
                    }
                ]
            }
        ),
    )
    with DB() as session:
        session.add(Tenant(id="tenant-a", name="Tenant A", quota=100))
        session.commit()
        result = asyncio.run(
            postal_provisioning.reconcile_live_domain_credentials(
                session, "tenant-a", ["mail.example.test"]
            )
        )
        credential = session.scalar(
            select(PostalDomainCredential).where(PostalDomainCredential.tenant_id == "tenant-a")
        )
        assert result["domains"] == ["mail.example.test"]
        assert credential.domain == "mail.example.test"
        assert credential.provider_mode == "Live"
        assert credential.state == "READY"
        assert credential.api_key_ciphertext != "test-provider-key-material-123456"
        assert (
            postal_provisioning.tenant_postal_api_key(session, "tenant-a", "mail.example.test")
            == "test-provider-key-material-123456"
        )


def test_live_domain_reconciliation_fails_closed_on_incomplete_attestation(monkeypatch):
    monkeypatch.setattr(
        postal_provisioning,
        "_call_live_domain_bridge",
        AsyncMock(return_value={"domains": []}),
    )
    with DB() as session:
        with pytest.raises(RuntimeError, match="incomplete"):
            asyncio.run(
                postal_provisioning.reconcile_live_domain_credentials(
                    session, "tenant-a", ["missing.example.test"]
                )
            )
