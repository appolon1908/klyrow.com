import base64
import asyncio
import json
import os
import time
import pytest

os.environ.update(
    KLYROW_DATABASE_URL="sqlite:///./test-provider.db",
    KLYROW_SESSION_SECRET="test-secret-provider-32-characters-minimum",
    KLYROW_SAFE_MODE="true",
    KLYROW_ENV="test",
    KLYROW_SANDBOX_DOMAIN="klyrow-sink.test",
)

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from apps.gateway.app.main import AllowedSender, Base, DB, Domain, InboundRouteConfig, Tenant, app, auth, engine
from apps.gateway.app.provider import DkimKey, ProviderDomain, ProviderEvent, ProviderInbound, ProviderMessage, ProviderUsageEvent, SandboxCapture, SenderIdentity, SmtpCredential, dispatch_provider_outbox, now, reconcile_provider_outbox_dead_letters, recover_expired_leases
from apps.gateway.app.secret_responses import SecretResponse
from apps.gateway.app.smtp_relay import GovernedRelay
from datetime import timedelta
from unittest.mock import AsyncMock, patch

client = TestClient(app)
identity = {"sub": "server-a-test", "tenant": "tenant-a", "role": "platform_admin", "service": True, "permissions": ["klyrow.send"]}


@pytest.fixture(autouse=True)
def provider_identity():
    app.dependency_overrides[auth] = lambda: identity
    yield
    app.dependency_overrides.pop(auth, None)


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        session.add_all([Tenant(id="tenant-a", name="A", quota=100), Tenant(id="tenant-b", name="B", quota=100)])
        session.add(Domain(id="legacy-domain", tenant_id="tenant-a", domain="codestra.co", token="verified", verified=True))
        session.add(AllowedSender(id="allowed-support", tenant_id="tenant-a", address="support@codestra.co", role="support", enabled=True))
        session.add(InboundRouteConfig(id="route-support", tenant_id="tenant-a", address="support@codestra.co", destination_kind="odoo_helpdesk", destination_ref="server-a:odoo-support", verified=True, enabled=True))
        session.commit()


def headers(key="provider-idempotency-1", correlation="provider-correlation-1"):
    return {"Idempotency-Key": key, "X-Correlation-Id": correlation}


def mail(**changes):
    payload = {"sender": "support@codestra.co", "recipient": "capture@klyrow-sink.test", "subject": "Sandbox", "text": "No Internet delivery", "stream": "TRANSACTIONAL", "sandbox": True}
    return {**payload, **changes}


def provision():
    made = client.post("/v1/internal/email/domains/register", json={"domain": "codestra.co"})
    domain_id = made.json()["id"]
    assert client.post("/v1/internal/email/domains/verify", params={"domain_id": domain_id}).json()["verified"] is True
    assert client.post("/v1/internal/email/senders", json={"domain_id": domain_id, "email": "support@codestra.co", "stream": "TRANSACTIONAL"}).status_code in {200, 201}
    assert client.put("/v1/internal/email/policy", json={"sending_disabled": True, "sandbox_mode": True, "daily_limit": 100, "hourly_limit": 100, "max_message_bytes": 100000, "max_attachment_bytes": 10000, "allowed_test_recipients": [], "reputation_state": "GOOD"}).status_code == 200
    return domain_id


def test_preflight_has_zero_queue_delta_and_blocks_internet():
    provision()
    with DB() as session:
        before = session.query(ProviderMessage).count()
    result = client.post("/v1/internal/email/preflight", headers={"X-Correlation-Id": "preflight-correlation"}, json=mail())
    assert result.status_code == 200 and result.json()["dry_run"] is True and result.json()["postal_submitted"] is False
    with DB() as session:
        assert session.query(ProviderMessage).count() == before
    assert client.post("/v1/internal/email/preflight", headers={"X-Correlation-Id": "blocked-correlation"}, json=mail(recipient="person@example.net")).status_code == 403


def test_send_idempotency_and_altered_replay():
    first = client.post("/v1/internal/email/send", headers=headers(), json=mail())
    assert first.status_code == 202 and first.json()["status"] == "QUEUED" and first.json()["sandbox"] is True, first.text
    replay = client.post("/v1/internal/email/send", headers=headers(), json=mail())
    assert replay.status_code == 202 and replay.json()["message_id"] == first.json()["message_id"]
    altered = client.post("/v1/internal/email/send", headers=headers(), json=mail(subject="Changed"))
    assert altered.status_code == 409


def test_sandbox_worker_captures_content_and_bills_once():
    result = client.post("/v1/internal/email/operations/process-sandbox")
    assert result.status_code == 200
    assert result.json()["provider"] == "internal-smtp-sink"
    with DB() as session:
        message = session.query(ProviderMessage).filter_by(idempotency_key="provider-idempotency-1").one()
        assert message.status == "DELIVERED"
        capture = session.query(SandboxCapture).filter_by(message_id=message.id).one()
        assert capture.envelope_to == "capture@klyrow-sink.test"
        assert "No Internet delivery" in capture.content_json
        assert session.query(ProviderUsageEvent).filter_by(message_id=message.id).count() == 1
        assert session.query(ProviderEvent).filter_by(message_id=message.id, kind="message.delivered").count() == 1
    assert client.post("/v1/internal/email/operations/process-sandbox").json()["count"] == 0
    with DB() as session:
        assert session.query(ProviderUsageEvent).filter_by(message_id=message.id).count() == 1


def test_suppression_size_filename_and_tenant_isolation():
    with DB() as session:
        from apps.gateway.app.main import Suppression
        session.add(Suppression(id="suppressed", tenant_id="tenant-a", email="capture@klyrow-sink.test", reason="hard_bounce"))
        session.commit()
    assert client.post("/v1/internal/email/preflight", headers={"X-Correlation-Id": "suppression-check"}, json=mail()).status_code == 422
    with DB() as session:
        session.query(Suppression).delete()
        session.commit()
    bad = mail(attachments=[{"filename": "../payload.exe", "content_type": "application/octet-stream", "size": 1}])
    assert client.post("/v1/internal/email/preflight", headers={"X-Correlation-Id": "filename-check"}, json=bad).status_code == 422
    identity["tenant"] = "tenant-b"
    assert client.post("/v1/internal/email/domains/register", json={"domain": "codestra.co"}).status_code == 409
    identity["tenant"] = "tenant-a"


def test_domain_and_sender_kill_switches_are_scoped():
    domain = client.post("/v1/internal/email/domains/register", json={"domain": "codestra.co"}).json()
    sender = client.post("/v1/internal/email/senders", json={"domain_id": domain["id"], "email": "support@codestra.co", "stream": "TRANSACTIONAL"}).json()
    assert client.post(f"/v1/internal/email/senders/{sender['id']}/suspend").json()["status"] == "SUSPENDED"
    assert client.post("/v1/internal/email/preflight", headers={"X-Correlation-Id": "sender-suspended"}, json=mail()).status_code == 403
    assert client.post(f"/v1/internal/email/domains/{domain['id']}/suspend").json()["status"] == "SUSPENDED"


def inbound_payload(event="postal-event-0001", filename="invoice.pdf"):
    raw = ("From: Sender <person@example.net>\r\nTo: support@codestra.co\r\n"
           f"Message-ID: <{event}@example.net>\r\nSubject: Help\r\nMIME-Version: 1.0\r\n"
           f"Content-Type: application/octet-stream; name=\"{filename}\"\r\n"
           f"Content-Disposition: attachment; filename=\"{filename}\"\r\n\r\ndata\r\n").encode()
    return {"provider_event_id": event, "envelope_to": "support@codestra.co", "raw_message_b64": base64.b64encode(raw).decode(),
        "spf_result": "PASS", "dkim_result": "PASS", "dmarc_result": "PASS",
        "arc_result": "NONE", "dmarc_fail_action": "REJECT"}


def test_inbound_exact_route_mime_idempotency_and_quarantine():
    first = client.post("/v1/internal/email/inbound/receive", json=inbound_payload())
    assert first.status_code == 202 and first.json()["disposition"] == "ACCEPT" and first.json()["duplicate"] is False
    assert client.post("/v1/internal/email/inbound/receive", json=inbound_payload()).json()["duplicate"] is True
    same_message = inbound_payload("postal-event-0002")
    raw = base64.b64decode(same_message["raw_message_b64"]).replace(b"<postal-event-0002@example.net>", b"<postal-event-0001@example.net>")
    same_message["raw_message_b64"] = base64.b64encode(raw).decode()
    assert client.post("/v1/internal/email/inbound/receive", json=same_message).json()["duplicate"] is True
    quarantined = client.post("/v1/internal/email/inbound/receive", json=inbound_payload("postal-event-0003", "payload.exe"))
    assert quarantined.json()["duplicate"] is False and quarantined.json()["disposition"] == "QUARANTINE"
    unknown = {**inbound_payload("postal-event-0004"), "envelope_to": "unknown@codestra.co"}
    assert client.post("/v1/internal/email/inbound/receive", json=unknown).status_code == 404
    assert client.post("/v1/internal/email/inbound/receive", json={**inbound_payload("postal-event-0005"), "destination_override": "https://attacker.invalid"}).status_code == 403
    with DB() as session:
        assert session.query(ProviderInbound).count() == 2
        accepted = session.query(ProviderInbound).filter_by(provider_event_id="postal-event-0001").one()
        event = session.query(ProviderEvent).filter_by(message_id=accepted.id, kind="inbound.received").one()
        body = json.loads(event.payload_json)
        assert body["destination_kind"] == "odoo_helpdesk"
        assert body["disposition"] == "ACCEPT"
        assert base64.b64decode(body["attachments"][0]["data_b64"]) == b"data\r\n"
        quarantined_item = session.query(ProviderInbound).filter_by(provider_event_id="postal-event-0003").one()
        assert session.query(ProviderEvent).filter_by(message_id=quarantined_item.id).count() == 0


def test_inbound_dangerous_filename_rejected():
    raw = b"From: x@example.net\r\nTo: support@codestra.co\r\nMessage-ID: <unsafe@example.net>\r\nContent-Disposition: attachment; filename=\"../x.exe\"\r\n\r\nx"
    payload = {**inbound_payload("postal-event-unsafe"), "raw_message_b64": base64.b64encode(raw).decode()}
    assert client.post("/v1/internal/email/inbound/receive", json=payload).status_code == 422


def test_inbound_authentication_verdict_is_required_and_dmarc_action_is_enforced():
    incomplete = inbound_payload("postal-event-auth-missing")
    incomplete.pop("dmarc_result")
    assert client.post("/v1/internal/email/inbound/receive", json=incomplete).status_code == 422
    rejected = {**inbound_payload("postal-event-auth-reject"), "dmarc_result": "FAIL", "dmarc_fail_action": "REJECT"}
    response = client.post("/v1/internal/email/inbound/receive", json=rejected)
    assert response.status_code == 202 and response.json()["disposition"] == "REJECT"
    with DB() as session:
        item = session.query(ProviderInbound).filter_by(provider_event_id="postal-event-auth-reject").one()
        assert (item.auth_verdict, item.spf_result, item.dkim_result, item.dmarc_result, item.arc_result) == ("FAIL", "PASS", "PASS", "FAIL", "NONE")


def test_postal_hash_inbound_requires_signature_timestamp_exact_route_and_replay_dedupe(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_path = tmp_path / "postal-inbound-public.pem"
    public_path.write_bytes(private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    os.environ["KLYROW_POSTAL_WEBHOOK_PUBLIC_KEY"] = str(public_path)

    def deliver(delivery_id: int, recipient: str, timestamp: float, *, signed: bool = True):
        attachment = b"same-name-content"
        payload = {
            "id": delivery_id,
            "rcpt_to": recipient,
            "mail_from": "person@example.net",
            "subject": "Signed inbound",
            "message_id": f"<postal-hash-{delivery_id}@example.net>",
            "timestamp": timestamp,
            "size": 512,
            "spam_status": "NotSpam",
            "bounce": False,
            "received_with_ssl": True,
            "to": recipient,
            "from": "Person <person@example.net>",
            "plain_body": "authenticated transport",
            "html_body": None,
            "attachment_quantity": 1,
            "attachments": [{
                "filename": "evidence.txt",
                "content_type": "text/plain",
                "size": len(attachment),
                "data": base64.encodebytes(attachment).decode(),
            }],
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json"}
        if signed:
            headers["X-Postal-Signature-256"] = base64.b64encode(private_key.sign(
                body, padding.PKCS1v15(), hashes.SHA256(),
            )).decode()
        return client.post("/v1/webhooks/postal-inbound", content=body, headers=headers)

    assert deliver(7001, "support@codestra.co", time.time(), signed=False).status_code == 401
    assert deliver(7002, "support@codestra.co", time.time() - 86_401).status_code == 401
    assert deliver(7003, "unknown@codestra.co", time.time()).status_code == 404
    first = deliver(7004, "support@codestra.co", time.time())
    assert first.status_code == 202
    assert first.json()["disposition"] == "QUARANTINE"
    assert first.json()["duplicate"] is False
    replay = deliver(7004, "support@codestra.co", time.time())
    # A different signed timestamp is a different body but the provider ID is
    # still the durable idempotency authority for this exact route.
    assert replay.status_code == 202 and replay.json()["duplicate"] is True
    with DB() as session:
        item = session.query(ProviderInbound).filter(
            ProviderInbound.provider_event_id == "postal:route-support:7004"
        ).one()
        assert session.query(ProviderEvent).filter_by(message_id=item.id, kind="inbound.received").count() == 0


def test_smtp_credential_once_rotation_revocation_and_tenant_isolation():
    with DB() as session:
        domain = session.query(ProviderDomain).filter_by(tenant_id="tenant-a", domain="codestra.co").one()
        domain.status = "VERIFIED"
        sender = session.query(SenderIdentity).filter_by(tenant_id="tenant-a", email="support@codestra.co").one()
        sender.status = "ACTIVE"
        session.commit()
        sender_id = sender.id
    with DB() as session:
        before_recovery_rows = session.query(SecretResponse).filter_by(resource_type="SMTP_CREDENTIAL").count()
    created = client.post("/v1/internal/email/smtp/credentials", json={"allowed_sender_ids": [sender_id], "allowed_streams": ["TRANSACTIONAL"], "expires_in_days": 30})
    assert created.status_code == 201 and created.json()["secret_display"] == "ONCE"
    assert "secret_response_id" not in created.json()
    credential = created.json()
    request = {"username": credential["username"], "password": credential["password"], "sender": "support@codestra.co", "recipient": "capture@klyrow-sink.test", "stream": "TRANSACTIONAL"}
    assert client.post("/v1/internal/email/smtp/preflight", json=request).json()["authorized"] is True
    with DB() as session:
        stored = session.get(SmtpCredential, credential["credential_id"])
        assert credential["password"] not in stored.secret_hash
    identity["tenant"] = "tenant-b"
    assert client.post("/v1/internal/email/smtp/preflight", json=request).status_code == 401
    identity["tenant"] = "tenant-a"
    rotated = client.post(f"/v1/internal/email/smtp/credentials/{credential['credential_id']}/rotate").json()
    assert rotated["secret_display"] == "ONCE"
    assert "secret_response_id" not in rotated
    assert rotated["password"] != credential["password"]
    with DB() as session:
        assert session.query(SecretResponse).filter_by(resource_type="SMTP_CREDENTIAL").count() == before_recovery_rows
    assert client.post("/v1/internal/email/smtp/preflight", json=request).status_code == 401
    request["password"] = rotated["password"]
    assert client.post("/v1/internal/email/smtp/preflight", json=request).status_code == 200
    assert client.post(f"/v1/internal/email/smtp/credentials/{credential['credential_id']}/revoke").status_code == 204
    assert client.post("/v1/internal/email/smtp/preflight", json=request).status_code == 401


def test_worker_lease_recovery_and_dead_letter():
    with DB() as session:
        base = dict(tenant_id="tenant-a", sender="support@codestra.co", recipient="capture@klyrow-sink.test",
            subject="lease", payload_json="{}", stream="TRANSACTIONAL", sandbox=True, request_hash="lease")
        retry = ProviderMessage(id="lease-retry", correlation_id="lease-retry-corr", idempotency_key="lease-retry-key",
            status="PROCESSING", attempts=1, lease_expires_at=now()-timedelta(seconds=1), **base)
        dead = ProviderMessage(id="lease-dead", correlation_id="lease-dead-corr", idempotency_key="lease-dead-key",
            status="PROCESSING", attempts=5, lease_expires_at=now()-timedelta(seconds=1), **base)
        session.add_all([retry, dead]);session.commit()
        assert recover_expired_leases(session) == 2
        assert session.get(ProviderMessage, "lease-retry").status == "INDETERMINATE"
        assert session.get(ProviderMessage, "lease-dead").status == "DEAD_LETTER"


def test_dkim_private_key_protection_and_rotation(tmp_path):
    with DB() as session:
        domain = session.query(ProviderDomain).filter_by(tenant_id="tenant-a", domain="codestra.co").one()
        domain.status = "VERIFIED"
        session.commit()
        domain_id = domain.id
    with patch.dict(os.environ, {"KLYROW_DKIM_KEY_DIR": str(tmp_path)}):
        first = client.post(f"/v1/internal/email/domains/{domain_id}/dkim/rotate")
        assert first.status_code == 201 and first.json()["private_key"] == "PROTECTED_SECRET_REFERENCE"
        assert "PRIVATE KEY" not in first.text and str(tmp_path) not in first.text
        with DB() as session:
            first_key = session.get(DkimKey, first.json()["dkim_key_id"])
            first_path = first_key.private_secret_ref.removeprefix("file:")
            public = first_key.public_value
        assert os.stat(first_path).st_mode & 0o777 == 0o600
        answer = type("Answer", (), {"strings": [public.encode()]})()
        with patch("apps.gateway.app.provider.dns.resolver.resolve", return_value=[answer]):
            assert client.post(f"/v1/internal/email/domains/{domain_id}/dkim/{first.json()['dkim_key_id']}/verify").json()["verified"] is True
        second = client.post(f"/v1/internal/email/domains/{domain_id}/dkim/rotate").json()
        with DB() as session:
            second_key = session.get(DkimKey, second["dkim_key_id"])
            answer = type("Answer", (), {"strings": [second_key.public_value.encode()]})()
        with patch("apps.gateway.app.provider.dns.resolver.resolve", return_value=[answer]):
            activated = client.post(f"/v1/internal/email/domains/{domain_id}/dkim/{second['dkim_key_id']}/verify").json()
        assert activated["verified"] is True and activated["retired_prior_keys"] == 1
        with DB() as session:
            assert session.get(DkimKey, first.json()["dkim_key_id"]).status == "RETIRED"


def test_domain_execution_dns_matrix_requires_single_spf_and_dmarc():
    with DB() as session:
        domain = session.query(ProviderDomain).filter_by(domain="codestra.co").one()
        domain.inbound_enabled = True
        domain.status = "VERIFIED"
        key = session.query(DkimKey).filter_by(domain_id=domain.id, status="ACTIVE").one()
        domain_id, selector, public = domain.id, key.selector, key.public_value
        session.commit()
    def txt(name):
        if name == "codestra.co": return ["v=spf1 include:spf.klyrow.com -all"]
        if name == "_dmarc.codestra.co": return ["v=DMARC1; p=reject"]
        if name == f"{selector}._domainkey.codestra.co": return [public]
        return []
    def values(name, kind):
        if name == "codestra.co" and kind == "MX": return ["10 mail.klyrow.com"]
        if name in {"bounce.codestra.co", "track.codestra.co"}: return ["37.27.128.39"]
        return []
    with patch("apps.gateway.app.provider.dns_txt", side_effect=txt), patch("apps.gateway.app.provider.dns_values", side_effect=values):
        result = client.post(f"/v1/internal/email/domains/{domain_id}/dns-check")
    assert result.status_code == 200 and result.json()["verified"] is True
    def duplicate_txt(name):
        if name == "codestra.co": return ["v=spf1 include:spf.klyrow.com -all", "v=spf1 -all"]
        return txt(name)
    with patch("apps.gateway.app.provider.dns_txt", side_effect=duplicate_txt), patch("apps.gateway.app.provider.dns_values", side_effect=values):
        result = client.post(f"/v1/internal/email/domains/{domain_id}/dns-check")
    assert result.json()["verified"] is False and result.json()["spf_record_count"] == 2


def test_event_and_usage_outbox_retries_without_double_billing():
    with patch("apps.gateway.app.main.emit_middleware", new=AsyncMock(return_value=False)):
        result = asyncio.run(dispatch_provider_outbox())
    assert result["events_failed"] >= 1
    with DB() as session:
        event = session.query(ProviderEvent).filter_by(kind="message.delivered").first()
        usage = session.query(ProviderUsageEvent).first()
        assert event.state == "RETRY" and usage.state == "RETRY"
        event.available_at = now()-timedelta(seconds=1)
        usage.available_at = now()-timedelta(seconds=1)
        usage_count = session.query(ProviderUsageEvent).count()
        session.commit()
    with patch("apps.gateway.app.main.emit_middleware", new=AsyncMock(return_value=True)) as emit:
        asyncio.run(dispatch_provider_outbox())
    usage_calls = [call for call in emit.await_args_list if call.args[0] == "klyrow.usage.recorded"]
    assert len(usage_calls) == 1
    assert usage_calls[0].args[1]["event_id"] == usage_calls[0].args[1]["usage_event_id"]
    with DB() as session:
        assert session.get(ProviderEvent, event.id).state == "DELIVERED"
        assert session.get(ProviderUsageEvent, usage.id).state == "DELIVERED"
        assert session.query(ProviderUsageEvent).count() == usage_count


def test_restricted_operations_retry_and_reconciliation():
    retried = client.post("/v1/internal/email/operations/messages/lease-dead/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "QUEUED"
    with DB() as session:
        event = session.query(ProviderEvent).filter_by(kind="message.delivered").first()
        event.state = "DEAD_LETTER"
        event.attempts = 8
        usage = session.query(ProviderUsageEvent).first()
        usage.state = "DEAD_LETTER"
        usage.attempts = 8
        session.commit()
        event_id = event.id
        usage_id = usage.id
    assert client.post(f"/v1/internal/email/operations/events/{event_id}/retry").json()["state"] == "RETRY"
    assert client.post(f"/v1/internal/email/operations/usage/{usage_id}/retry").json()["state"] == "RETRY"
    with DB() as session:
        assert session.get(ProviderEvent, event_id).attempts == 0
        assert session.get(ProviderUsageEvent, usage_id).attempts == 0
    reconciliation = client.post("/v1/internal/email/operations/reconcile")
    assert reconciliation.status_code == 200 and reconciliation.json()["status"] == "PASS"
    identity["role"] = "tenant_admin"
    assert client.post("/v1/internal/email/operations/reconcile").status_code == 403
    identity["role"] = "platform_admin"


def test_bounded_outbox_reconciliation_requeues_only_eligible_inbound():
    old = now() - timedelta(days=100)
    with DB() as session:
        session.add(InboundRouteConfig(
            id="route-local-only", tenant_id="tenant-a", address="local@codestra.co",
            destination_kind="webmail", destination_ref="klyrow:webmail", verified=True, enabled=True,
        ))
        eligible = ProviderInbound(
            id="legacy-inbound-eligible", tenant_id="tenant-a", provider_event_id="legacy-provider-eligible",
            route_id="route-support", message_id_header="<eligible@example.net>", sender="sender@example.net",
            recipient="support@codestra.co", subject="Eligible", text_body="body", html_body=None,
            attachments_json="[]", auth_verdict="PASS", spf_result="PASS", dkim_result="PASS",
            dmarc_result="PASS", arc_result="NONE", dmarc_fail_action="ACCEPT", disposition="ACCEPT",
        )
        local = ProviderInbound(
            id="legacy-inbound-local", tenant_id="tenant-a", provider_event_id="legacy-provider-local",
            route_id="route-local-only", message_id_header="<local@example.net>", sender="sender@example.net",
            recipient="local@codestra.co", subject="Local", text_body="body", html_body=None,
            attachments_json="[]", auth_verdict="PASS", spf_result="PASS", dkim_result="PASS",
            dmarc_result="PASS", arc_result="NONE", dmarc_fail_action="ACCEPT", disposition="ACCEPT",
        )
        lossy = ProviderInbound(
            id="legacy-inbound-lossy", tenant_id="tenant-a", provider_event_id="legacy-provider-lossy",
            route_id="route-support", message_id_header="<lossy@example.net>", sender="sender@example.net",
            recipient="support@codestra.co", subject="Lossy", text_body="body", html_body=None,
            attachments_json='[{"filename":"invoice.pdf","size":42}]', auth_verdict="PASS",
            spf_result="PASS", dkim_result="PASS", dmarc_result="PASS", arc_result="NONE",
            dmarc_fail_action="ACCEPT", disposition="ACCEPT",
        )
        session.add_all([eligible, local, lossy])
        session.add_all([
            ProviderEvent(
                id="legacy-event-eligible", tenant_id="tenant-a", message_id=eligible.id,
                kind="inbound.received", payload_json='{"event_id":"legacy-event-eligible"}',
                state="DEAD_LETTER", attempts=8, last_error="server_a_delivery_failed", created_at=old,
            ),
            ProviderEvent(
                id="legacy-event-local", tenant_id="tenant-a", message_id=local.id,
                kind="inbound.received", payload_json='{"event_id":"legacy-event-local"}',
                state="DEAD_LETTER", attempts=8, last_error="server_a_delivery_failed",
                created_at=old + timedelta(seconds=1),
            ),
            ProviderEvent(
                id="legacy-event-lossy", tenant_id="tenant-a", message_id=lossy.id,
                kind="inbound.received", payload_json='{"event_id":"legacy-event-lossy"}',
                state="DEAD_LETTER", attempts=8, last_error="server_a_delivery_failed",
                created_at=old + timedelta(seconds=2),
            ),
        ])
        session.commit()

        dry_run = reconcile_provider_outbox_dead_letters(
            session, tenant_id="tenant-a", limit=3, apply=False
        )
        assert dry_run["events_requeued"] == 1
        assert dry_run["events_skipped"] == 1
        assert dry_run["events_blocked"] == 1
        assert session.get(ProviderEvent, "legacy-event-eligible").state == "DEAD_LETTER"

        applied = reconcile_provider_outbox_dead_letters(
            session, tenant_id="tenant-a", limit=3, apply=True
        )
        session.commit()
        assert applied["events_requeued"] == 1
        assert applied["events_skipped"] == 1
        assert applied["events_blocked"] == 1
        requeued = session.get(ProviderEvent, "legacy-event-eligible")
        assert requeued.state == "RETRY" and requeued.attempts == 0
        assert json.loads(requeued.payload_json)["event"] == "inbound.received"
        assert session.get(ProviderEvent, "legacy-event-local").state == "SKIPPED"
        blocked = session.get(ProviderEvent, "legacy-event-lossy")
        assert blocked.state == "DEAD_LETTER"
        assert blocked.last_error == "operator_reconciliation_required"


def test_outbox_reconciliation_requires_confirmation_and_service_admin():
    missing_confirmation = client.post("/v1/internal/email/operations/outbox/reconcile", json={
        "reason": "repair production outbox", "limit": 1, "apply": True,
    })
    assert missing_confirmation.status_code == 422
    dry_run = client.post("/v1/internal/email/operations/outbox/reconcile", json={
        "reason": "inspect production outbox", "limit": 1, "apply": False,
    })
    assert dry_run.status_code == 200 and dry_run.json()["apply"] is False
    identity["role"] = "tenant_admin"
    try:
        assert client.post("/v1/internal/email/operations/outbox/reconcile", json={
            "reason": "inspect production outbox", "limit": 1, "apply": False,
        }).status_code == 403
    finally:
        identity["role"] = "platform_admin"


def test_spam_policy_accept_quarantine_and_reject():
    base = {"sending_disabled": True, "sandbox_mode": True, "daily_limit": 100,
        "hourly_limit": 100, "max_message_bytes": 100000, "max_attachment_bytes": 10000,
        "allowed_test_recipients": [], "reputation_state": "GOOD",
        "spam_quarantine_score": 5, "spam_reject_score": 10}
    assert client.put("/v1/internal/email/policy", json=base).status_code == 200
    accepted = inbound_payload("postal-event-spam-accept", "safe.txt")
    accepted["provider_spam_score"] = 4
    quarantined = inbound_payload("postal-event-spam-quarantine", "safe.txt")
    quarantined["provider_spam_score"] = 5
    rejected = inbound_payload("postal-event-spam-reject", "safe.txt")
    rejected["provider_spam_score"] = 10
    assert client.post("/v1/internal/email/inbound/receive", json=accepted).json()["disposition"] == "ACCEPT"
    assert client.post("/v1/internal/email/inbound/receive", json=quarantined).json()["disposition"] == "QUARANTINE"
    assert client.post("/v1/internal/email/inbound/receive", json=rejected).json()["disposition"] == "REJECT"


def test_tracking_tokens_are_opaque_tenant_scoped_expiring_and_idempotent():
    policy = {"sending_disabled": True, "sandbox_mode": True, "daily_limit": 100,
        "hourly_limit": 100, "max_message_bytes": 100000, "max_attachment_bytes": 10000,
        "allowed_test_recipients": [], "reputation_state": "GOOD", "tracking_mode": "OPEN_CLICK"}
    assert client.put("/v1/internal/email/policy", json=policy).status_code == 200
    with DB() as session:
        message = session.query(ProviderMessage).filter_by(tenant_id="tenant-a").first()
        message_id = message.id
    issued = client.post(f"/v1/internal/email/messages/{message_id}/tracking/OPEN")
    assert issued.status_code == 201
    token = issued.json()["token"]
    assert message_id not in token and "tenant-a" not in token and len(token) >= 32
    assert client.get(f"/t/OPEN/{token}").status_code == 204
    assert client.get(f"/t/OPEN/{token}").status_code == 204
    with DB() as session:
        assert session.query(ProviderEvent).filter_by(message_id=message_id, kind="message.opened").count() == 1
    identity["tenant"] = "tenant-b"
    assert client.post(f"/v1/internal/email/messages/{message_id}/tracking/OPEN").status_code == 404
    identity["tenant"] = "tenant-a"
    assert client.get("/t/OPEN/not-a-valid-token").status_code == 404


def test_smtp_rechecks_domain_stream_quota_and_suppression_at_submission():
    from apps.gateway.app.main import Suppression
    with DB() as session:
        domain = ProviderDomain(id="smtp-policy-domain", tenant_id="tenant-a", domain="smtp-policy.example",
            status="VERIFIED", ownership_token="smtp-policy-token")
        sender = SenderIdentity(id="smtp-policy-sender", tenant_id="tenant-a", domain_id=domain.id,
            email="sender@smtp-policy.example", stream="TRANSACTIONAL", status="ACTIVE")
        credential = SmtpCredential(id="smtp-policy-credential", tenant_id="tenant-a", username="smtp-policy-user",
            secret_hash="unused", allowed_senders_json='["sender@smtp-policy.example"]',
            allowed_streams_json='["TRANSACTIONAL"]', status="ACTIVE")
        session.add_all([domain, sender, credential, Suppression(id="smtp-policy-suppression", tenant_id="tenant-a",
            email="blocked@example.net", reason="policy")])
        session.commit()
    relay = GovernedRelay()
    smtp_session = type("SmtpSession", (), {"auth_data": "smtp-policy-credential"})()
    envelope = type("Envelope", (), {"mail_from": None, "rcpt_tos": [], "original_content":
        b"From: sender@smtp-policy.example\r\nTo: capture@klyrow-sink.test\r\nSubject: test\r\n\r\nbody"})()
    assert asyncio.run(relay.handle_MAIL(None, smtp_session, envelope, "sender@smtp-policy.example", [])) == "250 2.1.0 sender accepted"
    assert asyncio.run(relay.handle_RCPT(None, smtp_session, envelope, "blocked@example.net", [])) == "550 5.7.1 recipient suppressed"
    with DB() as session:
        session.get(ProviderDomain, "smtp-policy-domain").status = "SUSPENDED"
        session.commit()
    assert asyncio.run(relay.handle_MAIL(None, smtp_session, envelope, "sender@smtp-policy.example", [])) == "550 5.7.1 sender not authorized"
    with DB() as session:
        session.get(ProviderDomain, "smtp-policy-domain").status = "VERIFIED"
        session.get(SmtpCredential, "smtp-policy-credential").allowed_streams_json = '["BULK"]'
        session.commit()
    assert asyncio.run(relay.handle_DATA(None, smtp_session, envelope)) == "550 5.7.1 stream not authorized"


def test_private_metrics_cover_provider_integrations_deliverability_and_billing(tmp_path,monkeypatch):
    secret=tmp_path/"metrics-token";secret.write_text("private-metrics-test-token")
    monkeypatch.setenv("KLYROW_METRICS_TOKEN_FILE",str(secret))
    assert client.get("/metrics").status_code==404
    response=client.get("/metrics",headers={"Authorization":"Bearer private-metrics-test-token"})
    assert response.status_code==200
    names=("klyrow_provider_queue_messages","klyrow_email_outbox_oldest_seconds","klyrow_integration_outbox_items","klyrow_webhook_attempts","klyrow_delivery_ratio","klyrow_domain_dns_invalid","klyrow_billing_reconciliation_drift")
    assert all(name in response.text for name in names)
